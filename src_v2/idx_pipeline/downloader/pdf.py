from pathlib import Path

from idx_pipeline.utils.helper import (
    make_session, 
    open_json,
    write_json,
    random_sleep,
    parse_wib_datetime
)
from idx_pipeline.utils.constant import IDX_FORMAT_TITLE

import logging 
import re 
import requests 


LOGGER = logging.getLogger(__name__)


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

    output_path = output_dir / f'{original_filename}.pdf'

    with open(output_path, 'wb') as pdf_file:
        for chunk in response.iter_content(chunk_size=8192):
            pdf_file.write(chunk)

    return output_path 


def pdf_downloader(ingestion_result_path: str) -> list[dict]:
    raw_api_result = open_json(filename=ingestion_result_path)

    if raw_api_result is None or not raw_api_result: 
        return []

    session = make_session()

    downloader_ingestion = []
    
    output_dir = Path('data_v2/downloader')
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        for record in raw_api_result: 
            attachments = record.get('Attachments') or []
            publish_date = record.get('PublishDate')
            clean_publish_date = parse_wib_datetime(publish_date)
            clean_publish_date = clean_publish_date.strftime('%Y-%m-%d %H:%M:%S')

            if not attachments:
                continue 

            for attachment in attachments: 
                pdf_url = attachment.get('FullSavePath')
                original_filename = attachment.get('OriginalFilename').strip().lower().replace('.pdf', '')

                if not pdf_url: 
                    continue 

                output_path = download_doc(
                    session=session, 
                    pdf_url=pdf_url, 
                    output_dir=output_dir, 
                    original_filename=original_filename
                )

                random_sleep(1, 3)

                ingestion_record = {
                    'timestamp': clean_publish_date,
                    'pdf_local': str(output_path),
                    'pdf_url': pdf_url,
                    'type': 'idx' if is_idx_standard_layout(original_filename) else 'non_idx'
                }

                downloader_ingestion.append(ingestion_record)
    
    except Exception as error: 
        LOGGER.error('pdf downlaoder error %s', error, exc_info=True)
        return []

    download_ingestion_path = output_dir / 'downlod_ingestion.json'
    write_json(downloader_ingestion, str(download_ingestion_path))

    LOGGER.info(f'saved total pdf: {len(downloader_ingestion)}')

    return downloader_ingestion 


if __name__ == '__main__':
    downloader_ingestion = pdf_downloader('data_v2/ingestion/result.json')
    print(downloader_ingestion)

# uv run -m idx_pipeline.downloader.pdf  