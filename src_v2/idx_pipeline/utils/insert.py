from idx_pipeline.config.settings import SUPABASE_CLIENT

import logging 


LOGGER = logging.getLogger(__name__)


def push_db(payload: list[dict], table: str):
    if not payload: 
        LOGGER.info("[INSERT] %s: nothing to insert", table)
        return 

    if table == "idx_filings":
        for record in payload:
            for transaction in record.get("price_transaction") or []:
                transaction.pop("repurchase_agreement", None)

    try:
        response = (
            SUPABASE_CLIENT
            .table(table)
            .insert(payload)
            .execute()
        )

        LOGGER.info("[INSERT] %s: +%d", table, len(payload))
        return response
    
    except Exception as error:
        LOGGER.error("[INSERT] %s: insert failed: %s", table, error)
        raise


def update_db(replacements: list[dict], table: str):
    if not replacements:
        LOGGER.debug("database replacements are empty")
        return

    for replacement in replacements:
        database_id = replacement["database_id"]
        current_record = replacement["current_record"]

        if table == "idx_filings":
            for transaction in current_record.get("price_transaction") or []:
                transaction.pop("repurchase_agreement", None)

        try:
            (
                SUPABASE_CLIENT
                .table(table)
                .update(current_record)
                .eq("id", database_id)
                .execute()
            )

            LOGGER.info("[INSERT] %s: replaced record %s with its correction filing", table, database_id)

        except Exception as error:
            LOGGER.error(
                "[INSERT] %s: replacing record %s failed: %s",
                table,
                database_id,
                error,
            )
            raise
