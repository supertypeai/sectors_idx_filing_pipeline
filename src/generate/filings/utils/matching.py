from dotenv import load_dotenv
from supabase import create_client
from rapidfuzz import process, fuzz

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


def build_slug_lookup_investor(rows: list[dict], name_key: str) -> dict[str, str]:
    return {
        row[name_key].strip().lower(): row["slug"]
        for row in rows
        if row.get(name_key) and row.get("slug")
    }


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


def find_slug_investor(
    holder_name: str, 
    slug_lookup: dict[str, str], 
    threshold: int = 90
) -> str | None:
    candidates = list(slug_lookup.keys())

    result = process.extractOne(
        holder_name.strip().lower(),
        candidates,
        scorer=fuzz.token_sort_ratio, 
    )

    if result is None:
        return None

    matched_name, score, _ = result
    
    if score >= threshold:
        return slug_lookup[matched_name]
    return None


def matching_investor_and_conglomerates(idx_filings: list[FilingRecord]) -> list[FilingRecord]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        LOGGER.warning("[MATCHING] SUPABASE_URL/KEY missing; skip slug matching.")
        return idx_filings

    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)

        idx_investor = get_db(supabase_client, 'idx_investor')
        idx_conglomerates = get_db(supabase_client, 'idx_conglomerates_group')    

        investor_lookup = build_slug_lookup_investor(idx_investor, 'investor_name')
        conglomerate_lookup = build_slug_lookup(idx_conglomerates)

        for filing in idx_filings:
            filing_symbol = filing.symbol
            holder_name = filing.holder_name 
            
            investor_slug = find_slug_investor(holder_name=holder_name, slug_lookup=investor_lookup) if holder_name else None
            conglomerate_slug = conglomerate_lookup.get(filing_symbol) if filing_symbol else None


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