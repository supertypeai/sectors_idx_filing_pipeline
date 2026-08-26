from idx_pipeline.config.settings import SUPABASE_CLIENT

import logging 


LOGGER = logging.getLogger(__name__)


def push_db(payload: list[dict], table: str):
    if not payload: 
        LOGGER.info('payload is null: %s', len(payload))
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

        LOGGER.info("Inserted %d records into %s", len(payload), table)
        return response
    
    except Exception as error:
        LOGGER.error("Failed to insert into %s: %s", table, error)
        raise


def update_db(replacements: list[dict], table: str):
    if not replacements:
        LOGGER.info("database replacements are empty")
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

            LOGGER.info("Updated Correction record %s in %s", database_id, table)

        except Exception as error:
            LOGGER.error(
                "Failed to update record %s in %s: %s",
                database_id,
                table,
                error,
            )
            raise
