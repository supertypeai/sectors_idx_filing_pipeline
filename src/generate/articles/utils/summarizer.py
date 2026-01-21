# src/generate/articles/utils/summarizer.py
from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from typing import Dict, Tuple, Optional, Any, List
from pydantic import BaseModel, Field 

from src.common.log import get_logger
from src.generate.articles.model.llm_collection import LLMCollection

import time 


LOGGER = get_logger(__name__)


class SummaryResult(BaseModel): 
    """ 
    Result for summarized article. includes title and body.
    """
    title: str = Field(None, description="The summarized title.")
    body: str = Field(None, description="The summarized body text.")


def _fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return str(n)


def _coerce_flo(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _compose_rule_based(f: Dict[str, Any]) -> Tuple[str, str]:
    sym = f.get("symbol") or ""
    cname = f.get("company_name") or sym
    holder = f.get("holder_name") or f.get("holder_type") or "an insider"
    tx = (f.get("transaction_type") or "transaction").lower()

    hb, ha = f.get("holdings_before"), f.get("holdings_after")
    delta = ""
    try:
        if hb is not None and ha is not None:
            b, a = int(hb), int(ha)
            if a > b: delta = f", increasing holdings from {_fmt_int(b)} to {_fmt_int(a)}"
            elif a < b: delta = f", decreasing holdings from {_fmt_int(b)} to {_fmt_int(a)}"
            else: delta = f", resulting in {_fmt_int(a)} shares"
    except Exception:
        pass

    p = None
    prices = f.get("prices")
    if isinstance(prices, list) and prices:
        p = _coerce_flo(prices[0])
    if p is None:
        p = _coerce_flo(f.get("price"))
    p_phrase = f" at approximately IDR {int(round(p)): ,}".replace(",", ".") if p else ""

    title = f"{holder} {tx.title()} Transaction of {cname}"
    body = f"{holder} {tx} shares of {cname} ({sym}){p_phrase}{delta}. This filing was disclosed by the exchange."
    return title, body
    

def _facts_to_bullets(filings_data: Dict[str, Any]) -> str:
    lines: List[str] = []

    def add(key: str, value: str):
        if value is None: 
            return
        lines.append(f"- {key}: {value}")

    add("company_name", filings_data.get("company_name"))
    add("symbol", filings_data.get("symbol"))
    add("holder_name", filings_data.get("holder_name") or filings_data.get("holder_type"))
    add("transaction_type", filings_data.get("transaction_type"))
    add("purpose", filings_data.get("purpose_of_transaction"))

    add("price_transaction", filings_data.get("price_transaction"))

    add("amount_transaction", filings_data.get("amount_transaction"))
    add("holding_before", filings_data.get("holding_before"))
    add("holding_after", filings_data.get("holding_after"))

    add("timestamp", filings_data.get("timestamp"))
    # add("reason", filings_data.get("reason"))

    return "\n".join(lines)


class Summarizer:
    def __init__(self, use_llm: bool = False) -> None:
        # keep signature compat
        self.use_llm = use_llm
        self.llm_collection = LLMCollection()
        
    def create_prompt(self) -> ChatPromptTemplate: 
        return ChatPromptTemplate.from_messages(
            [
                (
                    'system', 
                    """ 
                    You are a financial analyst. Provide direct, concise summaries without any additional commentary or prefixes.
                    Focus on accuracy, clarity, and relevance to Indonesian stock market investors.
                    """
                ), 
                (
                    'user', 
                    """ 
                    Analyze this filing transaction and provide:
                    1. A title following this structure:
                        - if transaction type is sell or buy:
                            (Shareholder name) (Transaction type) Shares of (Company)
                        - if transaction type is others: 
                            (Company) Shareholder (holder_name) Reports New Transaction
                    2. A one-paragraph summary (max 150 tokens) focusing on: entities involved, transaction type, ownership changes, purpose, and significance.
                    
                    Filing:
                    {filings}

                    Note: 
                    - CRITICAL: If the transaction type is classified as 'others', do NOT state "described as others" or mention the category name. 
                        Instead, describe the specific underlying action as the transaction type.
                    - Keep it factual, don't speculate.
                    - Currency: IDR.
                    - Use thousands separator with comma (e.g., 83,420,100) and use dot for decimal separator.
                    - If prices exist, show one representative price like "IDR 490 per share".
                    - If holdings_before/after exist, show the transition and delta if clear.
                    
                    Return with the following structure JSON SummarizeResult and written in english.
                    {format_instructions}
                    """
                )
            ]
        )
    
    def summarize_from_facts(self, facts: Dict[str, Any]) -> Tuple[str, str]:
        if not self.use_llm:
            return _compose_rule_based(facts)

        filings_payload = _facts_to_bullets(facts)

        parser = JsonOutputParser(pydantic_object=SummaryResult)

        for llm in self.llm_collection.get_llms(): 
            try:
                chain = self.create_prompt() | llm | parser

                response = chain.invoke(
                    {
                        "filings": filings_payload,
                        "format_instructions": parser.get_format_instructions(),
                    }
                )

                if not response.get("title") or not response.get("body"):
                    LOGGER.info("[ERROR] LLM returned incomplete summary_result")
                    continue
                
                time.sleep(10)

                LOGGER.info(f'raw response: {response}')
                return response.get("title"), response.get("body")

            except Exception as error: 
                LOGGER.error(f'Error during LLM response: {error}, use next model') 
                continue 

        return _compose_rule_based(facts)


       