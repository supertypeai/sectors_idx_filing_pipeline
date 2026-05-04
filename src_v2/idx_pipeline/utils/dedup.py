from datetime import datetime, timedelta

from idx_pipeline.config.settings import SUPABASE_CLIENT 

import logging 


LOGGER = logging.getLogger(__name__)


def existing_keys(db_rows: list[dict]) -> set[tuple]:
    return {
        (
            row.get("symbol"),
            (row.get("timestamp") or "")[:10],   
            (row.get("holder_name") or "").lower(),
            row.get("transaction_type"),
            row.get("holding_before"),
            row.get("holding_after"),
            row.get("transaction_value")
        )
        for row in db_rows
    }


def row_key(row: dict) -> tuple:
    return (
        row.get("symbol"),
        (row.get("timestamp") or "")[:10],
        (row.get("holder_name") or "").lower(),
        row.get("transaction_type"),
        row.get("holding_before"),
        row.get("holding_after"),
        row.get("transaction_value")
    )


def get_payload_date_bounds(payload: list[dict]) -> tuple[str, str] | tuple[None, None]: 
    dates = sorted(
        {
            (row.get("timestamp") or "")[:10]
            for row in payload
            if row.get("timestamp")
        }
    )

    if not dates:
        return None, None
    
    start_date = dates[0]
    end_date_exclusive = (
        datetime.strptime(dates[-1], "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    return start_date, end_date_exclusive


def dedup_with_existing_db(payload: list[dict]) -> list[dict]:
    if not payload:
        LOGGER.info("dedup skipped: empty payload")
        return []
    
    start_date, end_date_exclusive = get_payload_date_bounds(payload)

    if not start_date:
        LOGGER.info("dedup skipped: payload has no timestamp")
        return payload
    
    LOGGER.info(f'date bounds dedup start: {start_date} end: {end_date_exclusive}')

    columns = (
        'symbol, timestamp, holder_name, '
        'transaction_type, holding_before, holding_after, '
        'transaction_value'
    )

    try: 
        response = (
            SUPABASE_CLIENT
            .table('idx_filings')
            .select(columns)
            .gte("timestamp", f"{start_date} 00:00:00")
            .lt("timestamp", f"{end_date_exclusive} 00:00:00")
            .execute()
        ) 
        LOGGER.info(f'response data db for dedup: {response.data}')

        existing_keys_set = existing_keys(response.data)
    
        clean_payload = [
            row 
            for row in payload
            if row_key(row) not in existing_keys_set
        ]

        if not clean_payload: 
            LOGGER.info(f'dedup with existing db return Null: {len(clean_payload)}')
            return []

        return clean_payload
    
    except Exception as error: 
        LOGGER.error('Dedup with existing db error %s', error)
        return []
    

if __name__ == '__main__':
    payload = [
  {
    "id": 1,
    "created_at": "2026-04-24 11:09:15.27908+00",
    "title": "Unitras Pertama sells shares of PT Saratoga Investama Sedaya Tbk",
    "body": "This is Unitras Pertama's 3rd insider disposal in the last 6 months, totaling a distribution of 2,760,000,000 shares transacted at an average price of IDR 1,770. Unitras Pertama's ownership in PT Saratoga Investama Sedaya Tbk has decreased from 31.623% to 24.841% in this period.",
    "source": "https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_KSEI/LK-24042026-7431-00.pdf-0.pdf",
    "timestamp": "2026-04-24 17:03:00",
    "sector": "financials",
    "sub_sector": "holding-investment-companies",
    "tags": [
      "divestment"
    ],
    "transaction_type": "sell",
    "holding_before": 4289610000,
    "holding_after": 3369610000,
    "amount_transaction": 920000000,
    "holder_type": "insider",
    "holder_name": "Unitras Pertama",
    "price": 1770.0,
    "transaction_value": 1628400000000,
    "price_transaction": [
      {
        "date": "2026-04-24",
        "type": "sell",
        "price": 1770,
        "amount_transacted": 920000000
      }
    ],
    "share_percentage_before": 31.623,
    "share_percentage_after": 24.841,
    "share_percentage_transaction": 6.782,
    "UID": None,
    "symbol": "SRTG.JK",
    "source_is_manual": False,
    "idx_investor_slug": None,
    "idx_conglomerates_group_slug": None,
    "context": None
  }
]

    clean = dedup_with_existing_db(payload)
    print(f'cleaned: {clean}')
