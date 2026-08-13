from idx_pipeline.utils.helper import get_db, write_json


def get_transaction_dates(record: dict) -> set[str]:
    return {
        transaction.get("date")
        for transaction in record.get("price_transaction") or []
        if transaction.get("date")
    }


def normalize_holder_name(record: dict) -> str:
    return (record.get("holder_name") or "").strip().lower()


def detect_filing_correction(records: list[dict]):
    records_sorted = sorted(
        records,
        key=lambda record: record.get("timestamp") or "",
        reverse=True
    )

    relationship_candidates = []
    related_payload_record_ids = set()

    for current_index, current_record in enumerate(records_sorted):
        current_timestamp = current_record.get("timestamp") or ""
        current_symbol = current_record.get("symbol")
        current_holder_name = normalize_holder_name(current_record)
        current_holding_before = current_record.get("holding_before")
        current_holding_after = current_record.get("holding_after")
        current_transaction_dates = get_transaction_dates(current_record)

        if not current_timestamp or not current_symbol:
            continue

        db_historical_records = get_db(
            table="idx_filings",
            columns=(
                "id, source, timestamp, symbol, holder_name, holding_before, "
                "holding_after, price_transaction, UID"
            ),
            query_modifier=lambda query: (
                query
                .eq("symbol", current_symbol)
                .eq("holder_name", current_record.get("holder_name"))
                .lt("timestamp", current_timestamp)
                .is_("UID", "null")
            ),
        )

        older_records = (
            records_sorted[current_index + 1:]
            + db_historical_records
        )

        for older_record in records_sorted[current_index + 1:]:
            older_timestamp = older_record.get("timestamp") or ""
            older_holding_before = older_record.get("holding_before")
            older_holding_after = older_record.get("holding_after")

            if not older_timestamp or older_timestamp >= current_timestamp:
                continue

            if older_record.get("symbol") != current_symbol:
                continue

            older_holder_name = normalize_holder_name(older_record)
            older_transaction_dates = get_transaction_dates(older_record)

            has_same_holder = bool(
                current_holder_name
                and current_holder_name == older_holder_name
            )

            has_overlapping_transaction_date = bool(
                current_transaction_dates & older_transaction_dates
            )

            has_same_holding_before = bool(
                current_holding_before is not None
                and current_holding_before == older_holding_before
            )

            has_same_holding_after = bool(
                current_holding_after is not None
                and current_holding_after == older_holding_after
            )

            if not (
                has_same_holder
                and has_overlapping_transaction_date
                and has_same_holding_before
                and has_same_holding_after
            ):
                continue

            relationship_candidates.append({
                "current_record": current_record,
                "older_record": older_record,
                "older_record_source": (
                    "db" if older_record.get("id") is not None else "payload"
                ),
            })

            related_payload_record_ids.add(id(current_record))

            if older_record.get("id") is None:
                related_payload_record_ids.add(id(older_record))

    distinct_records = [
        record
        for record in records_sorted
        if id(record) not in related_payload_record_ids
    ]

    return relationship_candidates, distinct_records


def resolve_correction_candidates(
    relationship_candidates: list[dict],
) -> tuple:
    current_records_by_identity = {}
    payload_older_record_ids = set()
    db_records_by_id = {}

    for candidate in relationship_candidates:
        current_record = candidate["current_record"]
        older_record = candidate["older_record"]
        older_record_source = candidate["older_record_source"]

        current_records_by_identity[id(current_record)] = current_record

        if older_record_source == "payload":
            payload_older_record_ids.add(id(older_record))

        elif older_record_source == "db":
            database_record_id = older_record["id"]
            existing_record = db_records_by_id.get(database_record_id)

            if (
                existing_record is None
                or current_record["timestamp"]
                > existing_record["current_record"]["timestamp"]
            ):
                db_records_by_id[database_record_id] = {
                    "database_id": database_record_id,
                    "current_record": current_record,
                }

    database_current_record_ids = {
        id(replacement["current_record"])
        for replacement in db_records_by_id.values()
    }

    payload_current_records = [
        current_record
        for current_record_identity, current_record
        in current_records_by_identity.items()
        if current_record_identity not in payload_older_record_ids
        and current_record_identity not in database_current_record_ids
    ]

    database_replacements = [
        replacement
        for replacement in db_records_by_id.values()
        if id(replacement["current_record"]) not in payload_older_record_ids
    ]

    return payload_current_records, database_replacements
    

if __name__ == "__main__":
    records = get_db(
        table="idx_filings",
        columns=(
            "id, source, timestamp, symbol, holder_name, source_is_manual, "
            "holding_before, holding_after, price_transaction, UID"
        ),
        query_modifier=lambda query: query.is_("UID", "null")
    )

    candidate, distinct = detect_filing_correction(records)
    # print(result)

    write_json(
        candidate, 
        "correction_results.json"
    )

# uv run -m idx_pipeline.parser.correction_detector 
