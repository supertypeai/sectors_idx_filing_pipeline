# src/generate/articles/utils/summarizer.py
from __future__ import annotations
from typing import Dict, Tuple, Optional, Any, List
from pydantic import BaseModel, Field 

import os
import json
import time 


# Optional: Gemini SDK
_GEMINI_OK = False
try:
    from google.genai import types
    from google import genai 
    _GEMINI_OK = True
except Exception:
    genai = None  # type: ignore


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


_SYS_PROMPT = """
    You are a precise financial writer. Write a concise, news-style title and a 2–4 sentence body from the given facts.
    - Keep it factual; don't speculate.
    - Currency: IDR.
    - Use thousands separator with comma (e.g., 83,420,100) and use dot for decimal separator.
    - If prices exist, show one representative price like "IDR 490 per share".
    - If holdings_before/after exist, show the transition and delta if clear.
    Return with the following structure JSON SummarizeResult.
"""


def _gemini_client():
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not _GEMINI_OK or not api_key:
        return None
    try:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(api_version='v1alpha') 
        )
        return client 
    except Exception:
        return None


def _get_llm_response(client: any, model: str, prompt_input: str) -> SummaryResult:
    try: 
        llm_response = client.models.generate_content( 
            model = model, 
            contents = [
                prompt_input
            ], 
            config = types.GenerateContentConfig(
                system_instruction=_SYS_PROMPT, 
                response_mime_type='application/json',
                response_schema=SummaryResult,
                temperature=0.4
            )
        )
        parsed_json = json.loads(llm_response.text)
        return parsed_json
    
    except Exception as error: 
        print(f"Error during LLM response parsing: {error}") 
        return SummaryResult(title="", body="")


def _facts_to_bullets(f: Dict[str, Any]) -> str:
    lines: List[str] = []
    def add(k,v):
        if v is None: return
        lines.append(f"- {k}: {v}")
    add("company_name", f.get("company_name"))
    add("symbol", f.get("symbol"))
    add("holder_name", f.get("holder_name") or f.get("holder_type"))
    add("transaction_type", f.get("transaction_type"))
    add("amount_transacted", sum(int(x) for x in (f.get("amount_transacted") or []) if x is not None) or f.get("amount_transaction"))
    add("prices", f.get("prices") or f.get("price"))
    add("holdings_before", f.get("holdings_before"))
    add("holdings_after", f.get("holdings_after"))
    add("timestamp", f.get("timestamp"))
    add("announcement_published_at", f.get("announcement_published_at"))
    add("reason", f.get("reason"))
    return "\n".join(lines)


class Summarizer:
    def __init__(self, use_llm: bool = False, groq_model: str = "", provider: Optional[str] = None) -> None:
        # keep signature compat
        self.use_llm = use_llm
        self.provider = (provider or os.getenv("LLM_PROVIDER") or "").strip().lower()
        self.model = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash") if self.provider == "gemini" else groq_model

        # Prepare Gemini client if chosen
        self._gem_client = None
        if self.provider == "gemini":
            self._gem_client = _gemini_client()

    def summarize_from_facts(self, facts: Dict[str, Any], text_hint: Optional[str] = None) -> Tuple[str, str]:
        if not self.use_llm:
            print('manual')
            return _compose_rule_based(facts)

        if self.provider == "gemini" and self._gem_client is not None:
            prompt = "\nFacts:\n" + _facts_to_bullets(facts)
            if text_hint:
                prompt += "\n\nContext:\n" + text_hint
            try:
                response = _get_llm_response(self._gem_client, self.model, prompt)
                print('use llm')
                print(f'raw response: {response}')
                title = str(response.get("title") or "").strip()
                body = str(response.get("body") or "").strip()

                time.sleep(12)
                if title and body:
                    return title, body
                
            except Exception:
                pass  # fall back if anything fails

        # Fallback to rule-based if Gemini is unavailable or failed
        return _compose_rule_based(facts)
