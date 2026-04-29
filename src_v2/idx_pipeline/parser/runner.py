from typing import Optional 

from .core import parser_new_document 
from idx_pipeline.utils.helper import open_json, write_json, parse_wib_datetime
from .utils.helper import *

import logging 
import re 


LOGGER = logging.getLogger(__name__)


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
    
    if not tags: 
        if transaction_type == 'buy':
            tags.add('investment')

        elif transaction_type == 'sell':
            tags.add('divestment')

    if 'investment' in tags and 'divestment' in tags: 
        if transaction_type == 'buy':
            tags.remove('divestment')

        elif transaction_type == 'sell':
            tags.remove('investment')

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
    # downloader_ingestion = open_json(downloader_path)
    company_lookup = open_json('data/company/company_map.json')

    payload_combined = []

    for record in downloader_ingestion: 
        pdf_local_path = record.get('pdf_local')
        pdf_url = record.get('pdf_url')
        timestamp = record.get('timestamp') 

        results = parser_new_document(
            pdf_local_path=pdf_local_path, 
            pdf_url=pdf_url,
            company_lookup=company_lookup
        )

        if results is None:
            print(f"parser_new_document returned None for: {pdf_local_path}")
            continue
        
        for result in results: 
            purpose = result.get('purpose')
            share_percentage_before = result.get('share_percentage_before')
            share_percentage_after = result.get('share_percentage_after')
            transaction_type = result.get('transaction_type')
            holder_name = result.get('holder_name')
            date = result.get('timestamp')
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
                existing_alerts = open_json('data_v2/alert/not_inserted.json') or []
                existing_alerts.append({
                    'date': date, 
                    'reasons': ['need to check manually if the document need UID generation'],
                    'source': source,
                    'symbol': symbol
                })
                write_json(existing_alerts, 'data_v2/alert/not_inserted.json')
                continue

            result['tags'] = tags
            result['holder_type'] = holder_type
            result['timestamp'] = timestamp 

        payload_combined.extend(results)

    filename = 'data_v2/parser/pdf_parsed.json'
    write_json(payload_combined, filename)

    return payload_combined


if __name__ == '__main__': 
    downloader_path = 'data_v2/downloader/downlod_ingestion.json'
    downloader_ingestion = open_json(downloader_path)

    payload = run_parser(downloader_ingestion=downloader_ingestion)
    print(payload)

# uv run -m idx_pipeline.parser.run_parser 