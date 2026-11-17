from pathlib import Path

from idx_pipeline.utils.helper import (
    make_session, 
    open_json,
    write_json,
    random_sleep,
    parse_wib_datetime
)

from idx_pipeline.utils.constant import MAX_DOWNLOAD_ATTEMPTS
from idx_pipeline.utils.log_setup import pdf_id, short_error

import logging 
import re 
import requests 


LOGGER = logging.getLogger(__name__)

PENDING_DOWNLOADS_PATH = Path("data_v2/state/pending_downloads.json")


def is_idx_standard_layout(original_filename: str) -> bool:
    return bool(re.match(r'^lk-\d{8}-\d+-\d+', original_filename.strip().lower()))


def download_doc(
    session: requests.Session,        
    pdf_url: str, 
    output_dir: str, 
    original_filename: str, 
): 
    response = session.get(pdf_url, stream=True)
    response.raise_for_status()

    safe_filename = original_filename.replace('/', '-').replace('\\', '-')

    if not safe_filename.lower().endswith('.pdf'):
        safe_filename = f'{safe_filename}.pdf'

    output_path = output_dir / safe_filename

    with open(output_path, 'wb') as pdf_file:
        for chunk in response.iter_content(chunk_size=8192):
            pdf_file.write(chunk)

    return output_path 


def collect_attachments(raw_api_result: list[dict]) -> list[dict]:
    """
    Flattens the announcement payload into one entry per PDF attachment
    """
    attachments_to_download = []

    for record in raw_api_result:
        attachments = record.get("Attachments") or []

        if not attachments:
            continue

        clean_publish_date = parse_wib_datetime(record.get("PublishDate"))

        for attachment in attachments:
            pdf_url = attachment.get("FullSavePath")

            if not pdf_url:
                continue

            original_filename = (
                (attachment.get("OriginalFilename") or Path(pdf_url).name)
                .strip()
                .lower()
                .replace(".pdf", "")
            )

            attachments_to_download.append({
                "timestamp": clean_publish_date.strftime("%Y-%m-%d %H:%M:%S"),
                "pdf_url": pdf_url,
                "original_filename": original_filename,
                "attempts": 0,
            })

    return attachments_to_download


def load_pending_downloads() -> list[dict]:
    if not PENDING_DOWNLOADS_PATH.exists():
        return []

    return open_json(str(PENDING_DOWNLOADS_PATH)) or []


def has_pending_downloads() -> bool:
    return bool(load_pending_downloads())


def save_pending_downloads(pending_downloads: list[dict]) -> None:
    PENDING_DOWNLOADS_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(pending_downloads, str(PENDING_DOWNLOADS_PATH))


def merge_with_pending(
    attachments: list[dict],
    pending_downloads: list[dict],
) -> list[dict]:
    """
    Puts the PDFs that failed in earlier runs ahead of this run's attachments,
    dropping repeats so a PDF is only downloaded once
    """
    merged = []
    seen_urls = set()

    for attachment in pending_downloads + attachments:
        pdf_url = attachment.get("pdf_url")

        if not pdf_url or pdf_url in seen_urls:
            continue

        seen_urls.add(pdf_url)
        merged.append(attachment)

    return merged


def pdf_downloader(
    ingestion_result_path: str,
    track_pending: bool = True,
) -> list[dict]:
    """
    Downloads every attachment in the ingestion payload.

    One PDF failing (usually an idx.co.id read timeout through the proxy) must not
    cost the rest of the batch. A failed PDF is skipped, and when track_pending is on
    it is saved to the state folder so the next run retries it. That matters because
    ingestion has already moved last_end past this window, so nothing else would ever
    go back for it.

    track_pending should be off for dry runs, so they neither consume nor add to
    the queue that the scheduled runs rely on.
    """
    raw_api_result = open_json(filename=ingestion_result_path) or []

    attachments = collect_attachments(raw_api_result)
    pending_downloads = load_pending_downloads() if track_pending else []

    if pending_downloads:
        LOGGER.info("[DOWNLOAD] retrying %d PDF(s) that failed in earlier runs", len(pending_downloads))

    attachments = merge_with_pending(attachments, pending_downloads)

    if not attachments:
        return []

    session = make_session()

    output_dir = Path('data_v2/downloader')
    output_dir.mkdir(parents=True, exist_ok=True)

    downloader_ingestion = []
    still_pending = []

    for attachment in attachments:
        pdf_url = attachment["pdf_url"]
        original_filename = attachment["original_filename"]

        try:
            output_path = download_doc(
                session=session,
                pdf_url=pdf_url,
                output_dir=output_dir,
                original_filename=original_filename,
            )

        except Exception as error:
            attempts = attachment.get("attempts", 0) + 1

            if not track_pending:
                LOGGER.warning(
                    "[DOWNLOAD] %s failed (%s), not queued (manual or dry run) - rerun this window",
                    pdf_id(pdf_url), short_error(error)
                )

            elif attempts >= MAX_DOWNLOAD_ATTEMPTS:
                LOGGER.error(
                    "[DOWNLOAD] %s failed %d times (%s), giving up - backfill it manually: %s",
                    pdf_id(pdf_url), attempts, short_error(error), pdf_url
                )
            else:
                LOGGER.warning(
                    "[DOWNLOAD] %s failed (%s), attempt %d of %d, queued for the next run",
                    pdf_id(pdf_url), short_error(error), attempts, MAX_DOWNLOAD_ATTEMPTS
                )
                still_pending.append({**attachment, "attempts": attempts})

            LOGGER.debug("download error for %s: %s", pdf_url, error)

            continue

        finally:
            random_sleep(1, 3)

        downloader_ingestion.append({
            "timestamp": attachment["timestamp"],
            "pdf_local": str(output_path),
            "pdf_url": pdf_url,
            "type": "idx" if is_idx_standard_layout(original_filename) else "non_idx"
        })

    if track_pending:
        save_pending_downloads(still_pending)

    download_ingestion_path = output_dir / 'downlod_ingestion.json'
    write_json(downloader_ingestion, str(download_ingestion_path))

    failed = len(attachments) - len(downloader_ingestion)

    LOGGER.info(
        "[DOWNLOAD] %d/%d PDF(s) downloaded | failed %d%s",
        len(downloader_ingestion), len(attachments), failed,
        f" (queued for retry: {len(still_pending)})" if track_pending else ""
    )

    return downloader_ingestion


if __name__ == '__main__':
    downloader_ingestion = pdf_downloader('data_v2/ingestion/result.json')
    print(downloader_ingestion)

# uv run -m idx_pipeline.downloader.pdf
