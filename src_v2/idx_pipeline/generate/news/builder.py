from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

from idx_pipeline.llm.client import get_llm
from idx_pipeline.llm.prompts import *
from idx_pipeline.utils.helper import random_sleep
from .utils.helper import fmt_idr, fmt_int, fmt_pct

import logging


LOGGER = logging.getLogger(__name__)


def format_price_transactions(price_transaction) -> str:
    if not price_transaction:
        return '-'
    
    if not isinstance(price_transaction, list):
        return fmt_idr(price_transaction)
    
    entries = []
    for item in price_transaction:
        tx_type = item.get('type', '-')
        amount = item.get('amount_transacted')
        price = item.get('price')
        date = item.get('date', '-')
        
        entries.append(
            f"{tx_type} {fmt_int(amount)} shares @ {fmt_idr(price)} on {date}"
        )
        
    return '; '.join(entries)


def format_filing_for_prompt(filing: dict) -> str:
    lines = [
        f"symbol: {filing.get('symbol') or '-'}",
        f"company name: {filing.get('company_name') or '-'}",
        f"holder name: {filing.get('holder_name') or '-'}",
        f"holder type: {filing.get('holder_type') or '-'}",
        f"transaction type: {filing.get('transaction_type') or '-'}",
        f"shares transacted: {fmt_int(filing.get('amount_transaction'))}",
        f"price transactions: {format_price_transactions(filing.get('price_transaction'))}",
        f"transaction value: {fmt_idr(filing.get('transaction_value'))}",
        f"holding before: {fmt_int(filing.get('holding_before'))}",
        f"holding after: {fmt_int(filing.get('holding_after'))}",
        f"ownership before: {fmt_pct(filing.get('share_percentage_before'))}",
        f"ownership after: {fmt_pct(filing.get('share_percentage_after'))}",
        f"timestamp: {filing.get('timestamp') or '-'}",
        f"purpose: {filing.get('purpose') or '-'}",
    ]
    return '\n'.join(lines)


def format_context_transactions(transactions: list[dict]) -> str:
    if not transactions:
        return 'none'

    headers = [
        'date', 'symbol', 'holder', 'type', 'shares',
        'price (IDR)', 'value (IDR)', 'ownership before', 'ownership after',
    ]

    rows = []
    for tx in transactions:
        rows.append([
            str(tx.get('timestamp') or '-')[:10],
            tx.get('symbol') or '-',
            tx.get('holder_name') or '-',
            tx.get('transaction_type') or '-',
            fmt_int(tx.get('amount_transaction')),
            fmt_int(tx.get('price')),
            fmt_int(tx.get('transaction_value')),
            fmt_pct(tx.get('share_percentage_before')),
            fmt_pct(tx.get('share_percentage_after')),
        ])

    col_widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(cell))

    def fmt_row(cells):
        return ' | '.join(cell.ljust(col_widths[idx]) for idx, cell in enumerate(cells))

    separator = '-+-'.join('-' * width for width in col_widths)
    lines = [fmt_row(headers), separator] + [fmt_row(row) for row in rows]
    return '\n'.join(lines)


def generate_news_title_body(record: dict):
    generation_parser = JsonOutputParser(pydantic_object=TitleBodyGeneration)
    format_instructions = generation_parser.get_format_instructions()

    prompt_collections = PomptCollections()
    system_prompt = prompt_collections.get_system_title_body_prompt()
    user_prompt = prompt_collections.get_user_title_body_prompt()

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ('user', user_prompt )
    ])

    for model in ['gpt-oss-120b', 'gpt-oss-20b', 'gemini-2.5-flash', 'llama-3.3-70b']:
        try:
            llm = get_llm(model, temperature=0.4)
            LOGGER.info(f"LLM used for news: {model}")

            context = record.get('context_data', {})

            formatted_context = format_context_transactions(context.get('transactions', []))
            formatted_current_filing =  format_filing_for_prompt(record)

            print(f'raw formatted context: {formatted_context}')
            print(f'raw formatted current filing: {formatted_current_filing}')

            input_data = {
                'current_filing': formatted_current_filing,
                'context_type': context.get('type', 'base'),
                'context_transactions': formatted_context,
                'format_instructions': format_instructions,
            }

            llm_chain = prompt | llm | generation_parser

            response = llm_chain.invoke(input_data)

            if response is None:
                LOGGER.warning("API call failed after all retries, trying next LLM...")
                continue

            if not response.get("title") or not response.get("body"):
                LOGGER.info("LLM news returned incomplete result")
                continue
            
            return response.get('title'), response.get('body')

        except Exception as error:
            LOGGER.warning(f"LLM failed with error: {error}", exc_info=True)
            continue  

    LOGGER.error("All LLMs failed to return a valid generation for news")
    return None


def clean_news_payload(
    record: dict, 
    title: str, 
    body: str, 
) -> dict:
    symbol = record.get('symbol', '')

    return {
        'title': title,
        'body': body,
        'source': record.get('source'),
        'timestamp': record.get('timestamp'),
        'sector': record.get('sector', ''),
        'sub_sector': [record.get('sub_sector', '')],
        'tags': ['Insider Trading'],
        'tickers': [symbol],
        'symbols': [symbol],
        'dimension': None,
        'votes': None,
        'score': None,
    }


def generate_news(payload: list[dict]) -> list[dict]:
    results = []

    for record in payload:
        result = generate_news_title_body(record)
        random_sleep(2, 5)

        if result is None:
            LOGGER.warning(f"Skipping news generation for {record.get('symbol')} — all LLMs failed")
            continue

        title, body = result
        cleaned = clean_news_payload(record, title, body)
        results.append(cleaned)

    return results


if __name__ == '__main__': 
    pass 


