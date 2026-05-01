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


LOGGER = logging.getLogger(__name__)


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
            title = record.get('Title') or ''
            attachments = record.get('Attachments') or []
            publish_date = record.get('PublishDate')
            clean_publish_date = parse_wib_datetime(publish_date)
            clean_publish_date = clean_publish_date.strftime('%Y-%m-%d %H:%M:%S')
            
            # later push this into alert/
            if title.strip() != IDX_FORMAT_TITLE:
                LOGGER.info('Non idx found, pushed it to alert/not_inserted.json')
                
                if len(attachments) == 0:
                    source = '-'

                elif len(attachments) == 1:
                    source = attachments[0].get('FullSavePath')

                else:
                    source = ', '.join(attachment.get('FullSavePath', '') for attachment in attachments)
                                
                not_inserted_payload = {
                    'date': clean_publish_date, 
                    'reasons': ['need to process manually, because pipeline do not process non-idx format document'],
                    'source': source,
                    'symbol': '-'
                }
                write_json([not_inserted_payload], 'data_v2/alert/not_inserted.json')
                continue 

            if not attachments:
                continue 

            for attachment in attachments: 
                pdf_url = attachment.get('FullSavePath')
                original_filename = attachment.get('OriginalFilename').strip().lower().replace('.pdf', '')

                if not pdf_url: 
                    continue 

                response = session.get(pdf_url, stream=True)
                response.raise_for_status()

                output_path = output_dir / f'{original_filename}.pdf'

                with open(output_path, 'wb') as pdf_file:
                    for chunk in response.iter_content(chunk_size=8192):
                        pdf_file.write(chunk)

                random_sleep(1, 3)

            ingestion_record = {
                'timestamp': clean_publish_date,
                'pdf_local': str(output_path),
                'pdf_url': pdf_url
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