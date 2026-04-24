from idx_pipeline.config.settings import SUPABASE_CLIENT 

import logging 


LOGGER = logging.getLogger(__name__)


def existing_keys(db_rows: list[dict]) -> set[tuple]:
    return {
        (
            row.get("symbol"),
            (row.get("timestamp") or "")[:10],   
            (row.get("holder_name") or "").lower(),
            row.get("transaction_type"),
            row.get("holding_before"),
            row.get("holding_after"),
            row.get("transaction_value")
        )
        for row in db_rows
    }


def row_key(row: dict) -> tuple:
    return (
        row.get("symbol"),
        (row.get("timestamp") or "")[:10],
        (row.get("holder_name") or "").lower(),
        row.get("transaction_type"),
        row.get("holding_before"),
        row.get("holding_after"),
        row.get("transaction_value")
    )


def dedup_with_existing_db(payload: list[dict]) -> list[dict]:
    columns = (
        'symbol, timestamp, holder_name, '
        'transaction_type, holding_before, holding_after, '
        'transaction_value'
    )

    try: 
        response = (
            SUPABASE_CLIENT
            .table('idx_filings')
            .select(columns)
            .execute()
        ) 

        existing_keys_set = existing_keys(response.data)
    
        clean_payload = [
            row 
            for row in payload
            if row_key(row) not in existing_keys_set
        ]

        if not clean_payload: 
            LOGGER.info(f'dedup with existing db return Null: {len(clean_payload)}')
            return []

        return clean_payload
    
    except Exception as error: 
        LOGGER.error('Dedup with existing db error %s', error)
        return []