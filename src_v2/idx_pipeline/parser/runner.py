from typing import Optional 

from .core import parser_new_document 
from .llm_parser import parser_with_llm
from idx_pipeline.utils.helper import open_json, write_json
from .utils.helper import *

import logging 
import re 


LOGGER = logging.getLogger(__name__)

ALERT_PATH = 'data_v2/alert/not_inserted.json'

# The LLM re-reads the document, so it can only help where the document holds the
# answer and our extraction fell short. A stale company_map and a filing that is
# genuinely about a non-common instrument are not extraction problems - re-parsing
# either one just burns a call and fails the same way.
NOT_RETRYABLE = (
    'company_map.json',
    'classification shares',
)


def push_alert(alert: dict):
    existing_alerts = open_json(ALERT_PATH) or []

    existing_alerts.append(alert)
    write_json(existing_alerts, ALERT_PATH)


def is_retryable(reasons: list[str]) -> bool:
    if not reasons:
        return False

    return not any(
        marker in reason
        for reason in reasons
        for marker in NOT_RETRYABLE
    )


def detect_tags(
    purpose: str,
    share_percentage_before: Optional[float],
    share_percentage_after: Optional[float],
    transaction_type: str,
) -> list[str]: 
    purpose = (purpose or '').lower()

    detect_tag = {
        "mesop": contains_any_keyword(purpose, KEYWORD_MESOP),
        "free_float_compliance": contains_any_keyword(purpose, KEYWORD_FREEFLOAT),
        "inheritance": contains_any_keyword(purpose, KEYWORD_INHERIT),
        "share-transfer": contains_any_keyword(purpose, KEYWORD_TRANSFER),
        'capital-restructuring': contains_any_keyword(purpose, KEYWORD_RESTRUCTURING),
        'investment': contains_any_keyword(purpose, KEYWORD_BUY),
        'divestment': contains_any_keyword(purpose, KEYWORD_SELL),
        'repurchase-agreement': contains_any_keyword(purpose, KEYWORD_REPURCHASE),
        'placement': contains_any_keyword(purpose, KEYWORD_PLACEMENT)
    }
    
    tags = set()

    for tag, found in detect_tag.items(): 
        if found: 
            tags.add(tag)
    
    if transaction_type == 'buy' and 'divestment' in tags:
        tags.discard('divestment')
        tags.add('investment')

    elif transaction_type == 'sell' and 'investment' in tags:
        tags.discard('investment')
        tags.add('divestment')

    if not tags:
        if transaction_type == 'buy':
            tags.add('investment')

        elif transaction_type == 'sell':
            tags.add('divestment')

    if crosses_50_percent_threshold(share_percentage_before, share_percentage_after):
        tags.add("takeover")

    tags = list(tags)
    return sorted(tags)


def detect_holder_type(holder_name: str) -> str:
    if not holder_name:
        return "insider"
    
    name_upper = re.sub(r"\s+", " ", holder_name).strip().upper()
    
    # Check for organization tokens
    for token in ORG_TOKENS:
        if token in name_upper:
            return "institution"
    
    # Check for common prefixes
    if re.search(r"\b(PT|CV|UD|YAYASAN|KOPERASI|BANK|SEKURITAS)\b", name_upper):
        return "institution"
    
    name_lower = holder_name.lower()

    if "pt" in name_lower or "tbk" in name_lower:
        return "institution"
    
    return "insider" 


def run_parser(downloader_ingestion: list[dict]):
    company_lookup = open_json('data_v2/idx_companies/company_map.json')

    payload_combined = []

    for record in downloader_ingestion: 
        pdf_local_path = record.get('pdf_local')
        pdf_url = record.get('pdf_url')
        timestamp = record.get('timestamp') 
        type_document = record.get('type')  # return only idx or non_idx

        LOGGER.info("parsing pdf_url: %s", pdf_url)
        
        if type_document == 'idx':
            results, reasons = parser_new_document(
                pdf_local_path=pdf_local_path,
                pdf_url=pdf_url,
                company_lookup=company_lookup,
                timestamp=timestamp
            )

            # The regex parser reads a fixed layout. When it comes up short the data
            # is still on the page, so hand the same PDF to the LLM before giving up
            if not results and is_retryable(reasons):
                LOGGER.info('regex parser failed, retrying with the llm: %s', pdf_url)

                results, reasons = parser_with_llm(
                    pdf_local_path=pdf_local_path,
                    pdf_url=pdf_url,
                    company_lookup=company_lookup,
                    timestamp=timestamp
                )

        elif type_document == 'non_idx':
            results, reasons = parser_with_llm(
                pdf_local_path=pdf_local_path,
                pdf_url=pdf_url,
                company_lookup=company_lookup,
                timestamp=timestamp
            )

        else:
            LOGGER.warning(f"unknown document type '{type_document}' for: {pdf_url}")
            continue

        if not results:
            # No reasons means nothing went wrong the holdings were unchanged, so
            # there was never a filing to insert
            if reasons:
                push_alert({
                    'date': timestamp or '-',
                    'reasons': reasons,
                    'source': pdf_url,
                    'symbol': '-'
                })

            LOGGER.info('no insertable results for %s: %s', pdf_url, reasons or 'holdings unchanged')
            continue

        for result in results: 
            purpose = result.get('purpose')
            share_percentage_before = result.get('share_percentage_before')
            share_percentage_after = result.get('share_percentage_after')
            transaction_type = result.get('transaction_type')
            holder_name = result.get('holder_name')
            source = result.get('source')
            symbol = result.get('symbol')

            tags = detect_tags(
                purpose=purpose,
                share_percentage_before=share_percentage_before, 
                share_percentage_after=share_percentage_after, 
                transaction_type=transaction_type
            )

            holder_type = detect_holder_type(holder_name=holder_name)
            
            is_share_transfer = 'share-transfer' in tags

            if is_share_transfer:
                push_alert({
                    'date': timestamp or '-',
                    'reasons': ['need to check manually if the document need UID generation'],
                    'tags': tags,
                    'holder_name': result['holder_name'],
                    'source': source,
                    'symbol': symbol
                })
                continue

            result['tags'] = tags
            result['holder_type'] = holder_type
            result['timestamp'] = timestamp

            payload_combined.append(result)

    filename = 'data_v2/parser/pdf_parsed.json'
    write_json(payload_combined, filename)

    return payload_combined


if __name__ == '__main__': 
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    
    downloader_path = 'data_v2/downloader/downlod_ingestion.json'
    downloader_ingestion = open_json(downloader_path)

    payload = run_parser(downloader_ingestion=downloader_ingestion)
    print(payload)


# uv run -m idx_pipeline.parser.runner