from dotenv import load_dotenv
from supabase import create_client

from src.core.types import FilingRecord

import os 
import logging


load_dotenv()


LOGGER = logging.getLogger("filings.pipeline")

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')


def get_db(client, table: str): 
    response = (
        client
        .table(table)
        .select('*')
        .execute()
    )
    return response.data or []


def get_idx_investor(client):
    result = get_db(client, 'idx_investor') 
    return result


def get_idx_conglomerates(client): 
    result = get_db(client, 'idx_conglomerates_group') 
    return result 


def build_slug_lookup(rows: list[dict]) -> dict[str, list[str]]:
    lookup = {}
    for row in rows: 
        slug = row.get('slug')
        symbols = row.get('symbol') or [] 

        for symbol in symbols: 
            if symbol not in lookup: 
                lookup[symbol] = []
            
            lookup[symbol].append(slug)

    return lookup


def matching_investor_and_conglomerates(idx_filings: list[FilingRecord]) -> list[FilingRecord]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        LOGGER.warning("[MATCHING] SUPABASE_URL/KEY missing; skip slug matching.")
        return idx_filings

    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)

        idx_investor = get_idx_investor(supabase_client)
        idx_conglomerates = get_idx_conglomerates(supabase_client)    

        investor_lookup = build_slug_lookup(idx_investor)
        conglomerate_lookup = build_slug_lookup(idx_conglomerates)

        for filing in idx_filings:
            filing_symbol = filing.symbol

            if not filing_symbol:
                continue
            
            investor_slug =  investor_lookup.get(filing_symbol)
            conglomerate_slug = conglomerate_lookup.get(filing_symbol)

            filing.idx_investor_slug = investor_slug
            filing.idx_conglomerates_group_slug = conglomerate_slug 

        return idx_filings

    except Exception as error: 
        LOGGER.warning(
            "[MATCHING] failed matching slug; skip enrichment. error=%s",
            error,
            exc_info=True,
        )
        return idx_filings