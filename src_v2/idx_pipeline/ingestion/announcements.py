from datetime import datetime, timedelta
from pathlib import Path

from idx_pipeline.utils.helper import (
    write_json, 
    open_json,
    make_session, 
    random_sleep,
    parse_wib_datetime,
    WIB
)
from idx_pipeline.utils.constant import (
    IDX_API, 
    DEFAULT_PAGE_SIZE, 
    DEFAULT_OVERLAP_MINUTES
)

import requests
import logging


LOGGER = logging.getLogger(__name__)

STATE_FILE = Path("data_v2/state/last_run.json")


def load_last_end() -> datetime | None:
    try:
        data = open_json(str(STATE_FILE))
        return parse_wib_datetime(data["last_end"])
    
    except Exception:
        return None


def save_last_end(end: datetime) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json({"last_end": end.isoformat()}, STATE_FILE)


def get_two_hour_window(overlap_minutes: int = DEFAULT_OVERLAP_MINUTES) -> tuple[datetime, datetime]:
    now_wib = datetime.now(WIB)
    last_end = load_last_end()
    
    if last_end is not None:
        return last_end - timedelta(minutes=overlap_minutes), now_wib

    return now_wib - timedelta(hours=2, minutes=overlap_minutes), now_wib


def get_window(
    start: str | None = None,
    end: str | None = None,
    overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
) -> tuple[datetime, datetime]:
    if start is None and end is None:
        return get_two_hour_window(overlap_minutes=overlap_minutes)

    if start is None or end is None:
        raise ValueError("start and end must both be provided, or both be omitted")

    start_dt = parse_wib_datetime(start)
    end_dt = parse_wib_datetime(end)

    # date-only input → cover full day
    if len(end.strip()) <= 10:
        end_dt = end_dt.replace(hour=23, minute=59, second=59)

    if end_dt < start_dt:
        raise ValueError("end must be >= start")

    return start_dt, end_dt


def is_within_window(
    publish_date_str: str,
    start: datetime,
    end: datetime,
) -> bool:
    publish_date = parse_wib_datetime(publish_date_str)
    return start <= publish_date <= end


def dedup_payload(records: list[dict]) -> list[dict]: 
    seen = set()
    clean_payload = []

    for record in records:
        key = (
            record.get("Id")
            or record.get("Title"),
            record.get("PublishDate"),
            record.get("Code"),
        )

        if key in seen:
            continue

        seen.add(key)
        clean_payload.append(record)

    return clean_payload 


def fetch_announcement(
    start_date: str,
    end_date: str,
    page: int,
    page_size: int,
    session: requests.Session,
    keyword: str
) -> dict:
    params = {
        "keywords": keyword,
        "pageNumber": page,
        "pageSize": page_size,
        "dateFrom": start_date,
        "dateTo": end_date,
        "lang": "en",
    }
    response = session.get(IDX_API, params=params)
    response.raise_for_status()
    return response.json()


def fetch_all_pages(
    start_date: str,
    end_date: str,
    page_size: int,
    session: requests.Session,
    keyword: str
) -> list[dict]:
    all_items = []
    page = 1

    while True:
        payload = fetch_announcement(
            start_date=start_date,
            end_date=end_date,
            keyword=keyword,
            page=page,
            page_size=page_size,
            session=session,
        )

        items = payload.get("Items", [])

        if not items:
            break

        all_items.extend(items)

        random_sleep(1, 3)

        if len(items) < page_size:
            break

        page += 1

    return all_items


def fetch_announcement_window(
    start: str | None = None,
    end: str | None = None,
    keyword: str = 'ownership',
    overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
) -> list[dict]:
    filename = 'data_v2/ingestion/result.json'
    is_auto = start is None and end is None  

    start_dt, end_dt = get_window(
        start=start,
        end=end,
        overlap_minutes=overlap_minutes,
    )
    
    LOGGER.info(f"start window: {start_dt} end window: {end_dt}")

    start_date = start_dt.strftime("%Y%m%d")
    end_date = end_dt.strftime("%Y%m%d")

    session = make_session()
    
    all_items = fetch_all_pages(
        start_date=start_date,
        end_date=end_date,
        keyword=keyword,
        page_size=DEFAULT_PAGE_SIZE,
        session=session,
    )

    if not all_items: 
        LOGGER.info(f"no announcements returned for {start_date}..{end_date}")
        write_json(payload=[], filename=filename)
        
        if is_auto:
            save_last_end(end_dt)

        return []
    
    payload =  [
        item for item in all_items
        if is_within_window(item["PublishDate"], start_dt, end_dt)
    ]
    
    clean_payload = dedup_payload(payload)

    LOGGER.info(f"total items fetched this session: {len(clean_payload)}")

    write_json(payload=clean_payload, filename=filename)

    if is_auto: 
        save_last_end(end_dt)

    return clean_payload


if __name__ == "__main__":
    fetch_announcement_window()

    # fetch_announcement_window(
    #     start="2026-04-23 08:00",
    #     end="2026-04-23 20:00",
    # )


# uv run -m idx_pipeline.ingestion.get_announcements                                                                                                                 