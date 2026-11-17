from typing import Annotated, Optional

from idx_pipeline.ingestion import announcements
from idx_pipeline.ingestion.announcements import fetch_announcement_window
from idx_pipeline.downloader.pdf import (
    pdf_downloader,
    has_pending_downloads,
    load_pending_downloads,
)
from idx_pipeline.parser.runner import run_parser
from idx_pipeline.parser.correction_detector import (
    detect_filing_correction,
    resolve_correction_candidates,
)
from idx_pipeline.generate.filings.builder import enrich
from idx_pipeline.generate.news.builder import generate_news
from idx_pipeline.utils.helper import open_json, write_json
from idx_pipeline.alerts.mailer import send_alert

from .utils.dedup import dedup_with_existing_db, dedup_within_payload
from .utils.insert import push_db, update_db
from .utils.helper import clean_payload
from .utils.log_setup import setup_logging, stage
from .fetcher.client import fetch_summary
from .utils.run_report import (
    RunSummary,
    log_run_summary,
    write_corrections,
    write_job_summary,
)

import typer 
import logging
import sys 


app = typer.Typer(
    help='A CLI for managing idx filing scraper',
    no_args_is_help=True
)


@app.callback()
def main():
    """
    News Scraper CLI.
    
    This callback function treats this as a multi-command app
    """
    setup_logging()


@app.command(name="run")
def run_pipeline(
    start_date: Annotated[Optional[str], typer.Option(help="Start date: YYYYMMDD or 'YYYY-MM-DD HH:MM'")] = None,
    end_date: Annotated[Optional[str], typer.Option(help="End date: YYYYMMDD or 'YYYY-MM-DD HH:MM'")] = None,
    is_push_db: Annotated[bool, typer.Option(help="Push records to database")] = True,
    is_send_alert: Annotated[bool, typer.Option(help="Send alert email")] = True,
    is_write_json: Annotated[bool, typer.Option(help="Write output to json")] = True
):
    logger = logging.getLogger(__name__)
    summary = RunSummary(pushed_to_db=is_push_db)

    # A dry or manual date-range run must not use up the retries the scheduled runs rely on
    is_auto = start_date is None and end_date is None
    track_pending = is_auto and is_push_db

    with stage(1, "INGEST", logger):
        ingestion_payload = fetch_announcement_window(start=start_date, end=end_date)

    if announcements.LAST_WINDOW:
        window_start, window_end = announcements.LAST_WINDOW
        summary.window = (
            f"{window_start:%Y-%m-%d %H:%M} → {window_end:%Y-%m-%d %H:%M} WIB"
        )

    summary.announcements = len(ingestion_payload or [])

    if not ingestion_payload and not (track_pending and has_pending_downloads()):
        logger.info("[INGEST] nothing new and no PDFs waiting for a retry, stopping here")
        summary.fetch = fetch_summary()
        write_job_summary(summary)
        return

    with stage(2, "DOWNLOAD", logger):
        ingestion_result_path = "data_v2/ingestion/result.json"
        downloader_ingestion = pdf_downloader(
            ingestion_result_path=ingestion_result_path,
            track_pending=track_pending,
        )

    summary.pdfs_downloaded = len(downloader_ingestion)
    summary.pdfs_queued = len(load_pending_downloads()) if track_pending else 0

    with stage(3, "PARSE & CHECK", logger):
        pdf_parsed_payload, flagged = run_parser(downloader_ingestion=downloader_ingestion)

        summary.filings_parsed = len(pdf_parsed_payload)

        logger.info(
            "[PARSE] %d PDF(s) → %d filing(s) | amended %d | kept as filed %d",
            len(downloader_ingestion),
            len(pdf_parsed_payload),
            sum(entry["status"] == "amended" for entry in flagged),
            sum(entry["status"] == "kept_as_filed" for entry in flagged),
        )

    with stage(4, "DEDUP & ENRICH", logger):
        payload_deduped = dedup_within_payload(payload=pdf_parsed_payload)

        correction_candidates, distinct_records = detect_filing_correction(
            records=payload_deduped
        )

        payload_current_records, database_replacements = resolve_correction_candidates(
            relationship_candidates=correction_candidates,
        )

        before_db_dedup = len(distinct_records)
        distinct_records = dedup_with_existing_db(payload=distinct_records)

        records_to_insert = distinct_records + payload_current_records

        summary.already_in_db = before_db_dedup - len(distinct_records)
        summary.replacements = len(database_replacements)

        logger.info(
            "[DEDUP] %d filing(s): %d repeated in this run, %d already in the DB, "
            "%d correction(s) of earlier filings → %d to insert",
            len(pdf_parsed_payload),
            len(pdf_parsed_payload) - len(payload_deduped),
            summary.already_in_db,
            len(database_replacements),
            len(records_to_insert),
        )

        database_current_records = [
            replacement["current_record"]
            for replacement in database_replacements
        ]

        database_record_ids = [
            replacement["database_id"]
            for replacement in database_replacements
        ]

        records_to_enrich = records_to_insert + database_current_records

        enrich(
            payload=records_to_enrich,
            excluded_filing_ids=database_record_ids
        )

        summary.highlights = sorted({
            record.get("symbol") for record in records_to_enrich
            if record.get("highlights")
        })

        logger.info(
            "[ENRICH] %d filing(s) enriched | highlights: %s",
            len(records_to_enrich),
            ", ".join(summary.highlights) or "none",
        )

    with stage(5, "NEWS", logger):
        news = generate_news(payload=records_to_insert)

        logger.info(
            "[NEWS] %d/%d article(s) generated", len(news), len(records_to_insert)
        )

    summary.news = len(news)

    filing_records_to_insert = clean_payload(payload=records_to_insert)
    summary.filings_to_insert = len(filing_records_to_insert)

    database_records_to_update = [
        {
            "database_id": replacement["database_id"],
            "current_record": clean_payload(
                payload=[replacement["current_record"]]
            )[0],
        }
        for replacement in database_replacements
    ]

    # Filings dropped as already in the DB were reported by the run that inserted them
    inserted_sources = {record.get("source") for record in records_to_insert}
    summary.flagged = [
        entry for entry in flagged if entry.get("source") in inserted_sources
    ]

    with stage(6, "ALERT & INSERT", logger):
        if is_send_alert:
            not_inserted_path = "data_v2/alert/not_inserted.json"
            existing_not_inserted = open_json(not_inserted_path) or []

            send_alert(
                payload_alert=existing_not_inserted,
                attachments_path=[not_inserted_path],
                mismatch_alerts=summary.kept_as_filed,
            )

        if is_write_json:
            payloads = {
                "filing_records_to_insert": filing_records_to_insert,
                "database_records_to_update": database_records_to_update,
                "news_records": news,
            }

            for name, payload in payloads.items():
                write_json(
                    payload=payload,
                    filename=f"{name}.json",
                )

        write_corrections(summary.flagged)

        if is_push_db:
            push_db(filing_records_to_insert, "idx_filings")
            update_db(database_records_to_update, "idx_filings")
            push_db(news, "idx_news")

        else:
            logger.info("[INSERT] dry run, nothing written to the DB")

    summary.fetch = fetch_summary()
    log_run_summary(summary, logger)
    write_job_summary(summary)


if __name__ == "__main__":
    app()


# uv run python -m idx_pipeline.pipeline run --no-is-send-alert --no-is-push-db --start-date 20260713 --end-date 20260713 
