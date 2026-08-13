from datetime import datetime
from calendar import monthrange
from pathlib import Path
from collections import defaultdict
from rapidfuzz import fuzz

from idx_pipeline.ingestion.announcements import (
    fetch_all_pages,
    make_session,
    DEFAULT_PAGE_SIZE
)
from idx_pipeline.utils.helper import open_json, write_json
from idx_pipeline.downloader.pdf import download_doc
from .utils.helper import normalize_holder_name

import pdfplumber
import logging
import re


LOGGER = logging.getLogger(__name__)

CACHE_PATH = 'data_v2/ingestion/monthly_securities.json'


def is_holder_detail_table(header: list[str]) -> bool:
    first_header = header[0].lower() if header else ''

    is_holder_section = any(
        section_name in first_header
        for section_name in ('pemegang saham', 'direksi', 'komisaris')
    )

    has_name_column = any(
        cell.strip().lower() == 'nama'
        for cell in header
    )

    has_current_shares_column = any(
        'jumlah saham' in cell.lower() and 'bulan' in cell.lower()
        for cell in header
    )

    return is_holder_section and has_name_column and has_current_shares_column


def clean_share_count(value: str) -> int | None:
    digits = re.sub(r'[^0-9]', '', value)

    return int(digits) if digits else None


def holder_names_match(
    filing_holder_name: str,
    report_holder_name: str,
    threshold: int,
) -> bool:
    normalized_filing_name = normalize_holder_name(filing_holder_name).casefold()
    normalized_report_name = normalize_holder_name(report_holder_name).casefold()

    if fuzz.WRatio(
        normalized_filing_name,
        normalized_report_name,
    ) > threshold:
        return True

    filing_tokens = set(normalized_filing_name.split())
    report_tokens = set(normalized_report_name.split())
    shorter_name_tokens = min(len(filing_tokens), len(report_tokens))

    if (
        shorter_name_tokens >= 2
        and (
            filing_tokens.issubset(report_tokens)
            or report_tokens.issubset(filing_tokens)
        )
    ):
        return True

    return False


def read_pdf(pdf_path: str) -> dict[str, int]:
    """
    Holder name -> shares held at the end of the month the report covers
    ('Jumlah Saham Bulan Ini'), summed across rows.
    """
    holders = defaultdict(int)

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''

            if 'go to indonesian page' in text.lower():
                break

            for table in page.extract_tables():
                if not table:
                    continue

                header = [
                    (cell or '').replace('\n', ' ')
                    for cell in table[0]
                ]

                if not is_holder_detail_table(header):
                    continue

                # Column positions shift between the 5% and Direksi/Komisaris
                # tables, so find the columns by header name instead of position.
                name_index = next(
                    (
                        index 
                        for index, cell in enumerate(header) 
                        if cell.strip().lower() == 'nama'
                    ),
                    None
                )

                shares_index = next(
                    (
                        index
                        for index, cell in enumerate(header)
                        if 'jumlah saham' in cell.lower() and 'bulan' in cell.lower()
                    ),
                    None
                )

                for row in table[1:]:
                    name = (row[name_index] or '').replace('\n', ' ').strip()

                    # Monthly reports use either Indonesian or English thousands
                    # separators, and long counts can wrap inside a cell.
                    shares = clean_share_count(row[shares_index] or '')

                    if not name or shares is None:
                        continue

                    # One holder can hold through several accounts, one row each
                    holders[name] += shares

    return dict(holders)


def fetch_securities_report(month: str, symbol: str) -> list[dict]:
    cache = open_json(CACHE_PATH) or {}
    cache_key = f'{month}:{symbol.upper()}'
    cached = cache.get(cache_key)
    exchange_code = symbol.split('.', 1)[0]

    today = datetime.today()
    today_str = today.strftime('%Y-%m-%d')
    is_current_month = month == today.strftime('%Y-%m')

    # Past months are complete. The current month keeps gaining reports as
    # companies file (they land around the 10th), so re-scrape it once a day
    if cached and (
        not is_current_month 
        or cached.get('scraped_at') == today_str
    ):
        return cached.get('items') or []

    start = datetime.strptime(month, '%Y-%m')
    last_day = monthrange(start.year, start.month)[1]
    start_date = start.strftime('%Y%m%d')
    
    end_date = (
        today.strftime('%Y%m%d')
        if is_current_month
        else start.replace(day=last_day).strftime('%Y%m%d')
    )

    items = fetch_all_pages(
        keyword=f'monthly securities {exchange_code}',
        start_date=start_date,
        end_date=end_date,
        page_size=DEFAULT_PAGE_SIZE,
        session=make_session()
    )

    cache[cache_key] = {'scraped_at': today_str, 'items': items}
    write_json(cache, CACHE_PATH)

    LOGGER.info(
        'securities report: cached %s items for %s in %s',
        len(items),
        symbol,
        month,
    )
    return items


def get_securities_data(
    symbol: str,
    holder_name: str,
    month: str,
    threshold: int = 95
) -> int | None:
    """
    Shares the holder held at the end of the month before their transaction,
    read from that company's monthly securities report. None if not found.
    """
    securities_records = fetch_securities_report(month, symbol)

    securities_records_sorted = sorted(
        securities_records,
        key=lambda record: record["PublishDate"],
        reverse=True
    )

    securities_source = None
    securities_name = None

    for record in securities_records_sorted:
        code = record.get("Code") or ""
        attachments = record.get("Attachments") or []

        if f"{code.strip().lower()}.jk" != symbol.lower():
            continue

        # IsAttachment 0 is the report itself, 1s are the lampiran
        for attachment in attachments:
            if attachment.get('IsAttachment') == 0:
                securities_source = attachment.get('FullSavePath')
                securities_name = attachment.get('OriginalFilename')
                break

        if securities_source is not None:
            break

    if not securities_source:
        LOGGER.warning(
            'no securities report for symbol: %s in %s (holder: %s)',
            symbol, month, holder_name
        )
        return None

    output_dir = Path('data_v2/downloader')
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = download_doc(
        session=make_session(),
        pdf_url=securities_source,
        output_dir=output_dir,
        original_filename=securities_name
    )

    holders = read_pdf(output_path)

    for name, shares in holders.items():
        if holder_names_match(holder_name, name, threshold):
            LOGGER.info(
                'securities report: %s -> %s shares (matched %s)',
                holder_name, shares, name
            )
            return shares

    LOGGER.warning(
        'holder %s not found in securities report for %s', holder_name, symbol
    )

    return None


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    holders = read_pdf('pdf_test/monthly_securities_lpkr.pdf')

    for name, shares in sorted(holders.items(), key=lambda item: -item[1]):
        print(f'{name:<30} {shares:>18,}')


# uv run -m idx_pipeline.parser.securities_report
