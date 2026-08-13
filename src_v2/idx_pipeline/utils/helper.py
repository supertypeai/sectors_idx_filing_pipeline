from pathlib import Path 
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from zoneinfo import ZoneInfo 

from idx_pipeline.config.settings import PROXY, SUPABASE_CLIENT
from .constant import HEADERS

import json 
import requests
import time 
import random 


WIB = ZoneInfo("Asia/Jakarta")


def random_sleep(start: float, end: float): 
    time.sleep(random.uniform(start, end))


def write_json(payload: list, filename:str): 
    filename_path = Path(filename)

    with filename_path.open('w') as file: 
        json.dump(payload, file, indent=2)


def open_json(filename: str):
    filename_path = Path(filename)

    with filename_path.open('r') as file:
        content = file.read().strip()

        if not content:
            return []
        
        return json.loads(content)


def clean_payload(payload: list[dict]) -> list[dict]:
    excluded_fields = {
        'company_name', 'purpose', 'split_variant', 
        'context_data', 'net_shares_transacted'
    }
    
    return [
        {field: value for field, value in record.items() if field not in excluded_fields}
        for record in payload
    ]


def parse_wib_datetime(raw: str) -> datetime:
    text = (raw or "").strip()

    if not text:
        raise ValueError("empty datetime string")

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))

    except Exception:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y%m%d", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break

            except ValueError:
                continue

        else:
            raise ValueError(f"invalid datetime: {raw!r}")

    if dt.tzinfo is None:
        return dt.replace(tzinfo=WIB)

    return dt.astimezone(WIB)


def make_session() -> requests.Session:
    session = requests.Session()
    session.verify = False
    session.headers.update(HEADERS)
    session.proxies.update({
        "http": PROXY,
        "https": PROXY,
    })

    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 504),
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


def get_db(
    table: str, 
    query_modifier,
    columns ="*",
) -> list[dict]:
    query = (
        SUPABASE_CLIENT
        .table(table)
        .select(columns)
    )

    if query_modifier is not None: 
        query = query_modifier(query)

    response = query.execute()
    return response.data 