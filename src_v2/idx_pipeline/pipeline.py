from typing import Annotated, Optional

from idx_pipeline.ingestion.announcements import fetch_announcement_window
from idx_pipeline.downloader.pdf import pdf_downloader
from idx_pipeline.parser.runner import run_parser
from idx_pipeline.generate.filings.builder import enrich
from idx_pipeline.generate.news.builder import generate_news
from idx_pipeline.alerts.filter import get_data_alert
from idx_pipeline.utils.helper import write_json, open_json
from idx_pipeline.alerts.mailer import send_alert

from .utils.dedup import dedup_with_existing_db
from .utils.insert import push_db
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
            logging.StreamHandler(sys.stdout)
            # logging.FileHandler("scraper.log") 
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
    is_push_db: Annotated[bool, typer.Option(help="Push records to database")] = True
):
    fetch_announcement_window(start=start_date, end=end_date)

    ingestion_result_path = 'data_v2/ingestion/result.json'
    downloader_ingestion = pdf_downloader(ingestion_result_path=ingestion_result_path)

    pdf_parsed_payload = run_parser(downloader_ingestion=downloader_ingestion)
    
    payload = dedup_with_existing_db(payload=pdf_parsed_payload)

    payload_enriched = enrich(payload=payload)

    payload_inserted, payload_not_inserted = get_data_alert(payload=payload_enriched)

    not_inserted_path = 'data_v2/alert/not_inserted.json'
    existing_not_inserted = open_json(not_inserted_path)
    accumulated = existing_not_inserted + payload_not_inserted

    if accumulated:
        write_json(accumulated, not_inserted_path)
        send_alert(payload_alert=accumulated, attachments_path=[not_inserted_path])
        write_json([], not_inserted_path)

    # news
    # news = generate_news(payload=payload_inserted) 
    
    # filing
    filing = clean_payload(payload=payload_inserted)

    print(f'cleaned payload filing: {filing}')
    # print(f'\ncleaned payload news: {news}')
    
    if is_push_db:
        push_db(filing, 'idx_filings_test')
    #     push_db(news, 'idx_news')


if __name__ == "__main__":
    app()


# uv run python -m idx_pipeline.pipeline run
