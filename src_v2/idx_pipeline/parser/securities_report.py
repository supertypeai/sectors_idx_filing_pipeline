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
from .utils.helper import normalize_holder_name, clean_number

import pdfplumber
import logging


LOGGER = logging.getLogger(__name__)

CACHE_PATH = 'data_v2/ingestion/monthly_securities.json'


def read_pdf(pdf_path: str) -> dict[str, int]:
    """
    Holder name -> shares held at the end of the month the report covers
    ('Jumlah Saham Bulan Ini'), summed across rows.
    """
    holders = defaultdict(int)

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[1:]:
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

                # Column positions shift between the two tables - the Direksi one
                # carries an extra 'Jabatan' - so find them by name, never by index
                name_index = next(
                    (
                        index 
                        for index, cell in enumerate(header) 
                        if cell.strip() == 'Nama'
                    ),
                    None
                )

                shares_index = next(
                    (index for index, cell in enumerate(header)
                     if 'Jumlah' in cell and 'Bulan' in cell),
                    None
                )

                if name_index is None or shares_index is None:
                    continue

                for row in table[1:]:
                    name = (row[name_index] or '').replace('\n', ' ').strip()

                    # A long number wraps inside its cell: '11.259.645.29\n0'
                    shares = clean_number((row[shares_index] or '').replace('\n', ''))

                    if not name or not shares:
                        continue

                    # One holder can hold through several accounts, one row each
                    holders[name] += shares

    return dict(holders)


def fetch_securities_report(month: str) -> list[dict]:
    cache = open_json(CACHE_PATH) or {}
    cached = cache.get(month)

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
        keyword='monthly securities',
        start_date=start_date,
        end_date=end_date,
        page_size=DEFAULT_PAGE_SIZE,
        session=make_session()
    )

    cache[month] = {'scraped_at': today_str, 'items': items}
    write_json(cache, CACHE_PATH)

    LOGGER.info('securities report: cached %s items for %s', len(items), month)

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
    securities_records = fetch_securities_report(month)

    securities_source = None
    securities_name = None

    for record in securities_records:
        code = record.get('Code') or ''
        attachments = record.get('Attachments') or []

        if f'{code.lower()}.jk' != symbol.lower():
            continue

        # IsAttachment 0 is the report itself, 1s are the lampiran
        for attachment in attachments:
            if attachment.get('IsAttachment') == 0:
                securities_source = attachment.get('FullSavePath')
                securities_name = attachment.get('OriginalFilename')
                break

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

    target_name = normalize_holder_name(holder_name)

    for name, shares in holders.items():
        score = fuzz.WRatio(target_name, normalize_holder_name(name))

        if score > threshold:
            LOGGER.info(
                'securities report: %s -> %s shares (matched %s, score %s)',
                holder_name, shares, name, round(score)
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
