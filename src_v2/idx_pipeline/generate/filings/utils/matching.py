from rapidfuzz import process, fuzz

import logging
import re 


LOGGER = logging.getLogger(__name__)


def clean_name_titles(name: str) -> str:
    # Remove prefix titles
    name = re.sub(
        r'^(Ir|Drs?|Dra)\.?\s*',
        '',
        name,
        flags=re.IGNORECASE
    )

    # Remove suffix titles
    name = re.sub(
        r',?\s*(Ir|Drs?|Dra)\.?$',
        '',
        name,
        flags=re.IGNORECASE
    )

    # Convert "A.B" -> "A B"
    name = re.sub(r'\.([a-zA-Z])(?=\s|$)', r' \1', name)

    # Convert " A." -> " A"
    name = re.sub(r'(?<=\s)([a-zA-Z])\.', r'\1', name)

    # Replace punctuation with spaces
    name = re.sub(r'[-.,/()]', ' ', name)

    return ' '.join(name.lower().split())


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
    threshold: int = 96,
) -> dict | None:
    cleaned_lookup = {
        clean_name_titles(investor_name): investor_name
        for investor_name in investor_lookup
    }

    clean_holder_name = clean_name_titles(holder_name.lower().strip())

    result = process.extractOne(
        clean_holder_name,
        cleaned_lookup.keys(),
        scorer=fuzz.WRatio,
    )

    if result is None:
        return None
    
    matched_name, score, _ = result
    
    if score >= threshold:
        original_name = cleaned_lookup[matched_name]

        LOGGER.info('raw matching: %s | holder name filing: %s', result, clean_holder_name)
        return investor_lookup[original_name]
    
    return None


def find_conglomerate_slug(
    group_name: str,
    conglomerate_name_lookup: dict[str, str],
    threshold: int = 96,
) -> str | None:
    candidates = list(conglomerate_name_lookup.keys())
    
    result = process.extractOne(
        group_name.lower().strip(),
        candidates,
        scorer=fuzz.WRatio,
    )

    if result is None:
        return None
    
    matched_name, score, _ = result

    if score >= threshold:
        return conglomerate_name_lookup[matched_name]
    
    return None


def matching_investor_and_conglomerates(
    filing: dict, 
    idx_investor: list, 
    idx_conglomerates: list
) -> dict:
    try:
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
    

if __name__ == '__main__':
    result = find_investor(
        holder_name='Drs Lo Kheng Hong', 
        investor_lookup={
            'Lo Kheng Hong': {
                'slug': 'test'
            }
        }
    )
    print(result)
