from rapidfuzz import process, fuzz

from idx_pipeline.config.settings import SUPABASE_CLIENT 

import logging


LOGGER = logging.getLogger(__name__)


def get_db(client, table: str): 
    response = (
        client
        .table(table)
        .select('*')
        .execute()
    )
    return response.data or []


def build_investor_lookup(rows: list[dict], name_key: str) -> dict[str, dict]:
    return {
        row[name_key].strip().lower(): {
            'slug': row.get('slug'),
            'group': row.get('group'),
        }
        for row in rows
        if row.get(name_key) and row.get('slug')
    }


def build_conglomerate_name_lookup(rows: list[dict]) -> dict[str, str]:
    return {
        row['name'].strip().lower(): row['slug']
        for row in rows
        if row.get('name') and row.get('slug')
    }


def find_investor(
    holder_name: str,
    investor_lookup: dict[str, dict],
    threshold: int = 90,
) -> dict | None:
    candidates = list(investor_lookup.keys())
    result = process.extractOne(
        holder_name.strip().lower(),
        candidates,
        scorer=fuzz.token_sort_ratio,
    )

    if result is None:
        return None
    
    matched_name, score, _ = result
    if score >= threshold:
        return investor_lookup[matched_name]
    return None


def find_conglomerate_slug(
    group_name: str,
    conglomerate_name_lookup: dict[str, str],
    threshold: int = 85,
) -> str | None:
    candidates = list(conglomerate_name_lookup.keys())
    result = process.extractOne(
        group_name.strip().lower(),
        candidates,
        scorer=fuzz.token_sort_ratio,
    )
    if result is None:
        return None
    matched_name, score, _ = result
    if score >= threshold:
        return conglomerate_name_lookup[matched_name]
    return None


def matching_investor_and_conglomerates(filing: dict) -> dict:
    try:
        idx_investor = get_db(SUPABASE_CLIENT, 'idx_investor')
        idx_conglomerates = get_db(SUPABASE_CLIENT, 'idx_conglomerates_group')

        investor_lookup = build_investor_lookup(idx_investor, 'investor_name')
        conglomerate_name_lookup = build_conglomerate_name_lookup(idx_conglomerates)

        holder_name = filing.get('holder_name')

        investor_slug = None
        conglomerate_slug = None

        if holder_name:
            investor = find_investor(holder_name=holder_name, investor_lookup=investor_lookup)
            if investor:
                investor_slug = investor.get('slug')
                group = investor.get('group')

                if group:
                    conglomerate_slug = find_conglomerate_slug(
                        group_name=group,
                        conglomerate_name_lookup=conglomerate_name_lookup,
                    )

        filing['idx_investor_slug'] = investor_slug
        filing['idx_conglomerates_group_slug'] = conglomerate_slug

        return filing

    except Exception as error:
        LOGGER.warning(
            "[MATCHING] failed matching slug; skip enrichment. error=%s",
            error,
            exc_info=True,
        )
        return filing