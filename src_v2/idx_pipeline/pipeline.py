from typing import Annotated, Optional

from idx_pipeline.ingestion.announcements import fetch_announcement_window
from idx_pipeline.downloader.pdf import pdf_downloader
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

import typer 
import logging
import sys 


def setup_logging():
    """
    Configures logging for the whole application
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("pipeline.log") 
        ]
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


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

    ingestion_payload = fetch_announcement_window(start=start_date, end=end_date)

    if not ingestion_payload:
        logger.info(
            "No announcements in the requested window; skipping downstream stages."
        )
        return
    
    ingestion_result_path = 'data_v2/ingestion/result.json'
    downloader_ingestion = pdf_downloader(ingestion_result_path=ingestion_result_path)
    
    pdf_parsed_payload = run_parser(downloader_ingestion=downloader_ingestion)
    
    logger.info('length before dedup: %d', len(pdf_parsed_payload))

    payload_deduped = dedup_within_payload(payload=pdf_parsed_payload)

    correction_candidates, distinct_records = detect_filing_correction(
        records=payload_deduped
    )

    logger.info(
        "current-payload relationship candidates: %d | distinct records: %d",
        len(correction_candidates),
        len(distinct_records),
    )

    payload_current_records, database_replacements = resolve_correction_candidates(
        relationship_candidates=correction_candidates,
    )

    distinct_records = dedup_with_existing_db(payload=distinct_records)

    records_to_insert = distinct_records + payload_current_records

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

    news = generate_news(payload=records_to_insert)

    filing_records_to_insert = clean_payload(payload=records_to_insert)

    database_records_to_update = [
        {
            "database_id": replacement["database_id"],
            "current_record": clean_payload(
                payload=[replacement["current_record"]]
            )[0],
        }
        for replacement in database_replacements
    ]

    logger.info(
        "records to insert: %d | database records to update: %d",
        len(filing_records_to_insert),
        len(database_records_to_update),
    )

    if is_send_alert:
        not_inserted_path = 'data_v2/alert/not_inserted.json'
        existing_not_inserted = open_json(not_inserted_path) or []

        send_alert(
            payload_alert=existing_not_inserted,
            attachments_path=[not_inserted_path]
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

    if is_push_db:
        push_db(filing_records_to_insert, "idx_filings")
        update_db(database_records_to_update, "idx_filings")
        push_db(news, "idx_news")


if __name__ == "__main__":
    app()


# uv run python -m idx_pipeline.pipeline run --no-is-send-alert --no-is-push-db --start-date 20260713 --end-date 20260713 
