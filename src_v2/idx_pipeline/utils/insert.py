from idx_pipeline.config.settings import SUPABASE_CLIENT

import logging 


LOGGER = logging.getLogger(__name__)


def push_db(payload: list[dict], table: str):
    try:
        response = (
            SUPABASE_CLIENT
            .table(table)
            .insert(payload)
            .execute()
        )

        LOGGER.info("Inserted %d records into %s", len(payload), table)
        return response
    
    except Exception as error:
        LOGGER.error("Failed to insert into %s: %s", table, error)
        raise