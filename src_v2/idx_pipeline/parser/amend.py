from idx_pipeline.config.settings import SUPABASE_CLIENT
from .securities_report import get_securities_data
from .utils.helper import enrich_transaction

import logging


LOGGER = logging.getLogger(__name__)

# Share percentages are reported to 2 decimals, so the smallest change the document
# can express is 0.01% of shares outstanding. Any mismatch below that is invisible
# to the percentage, and it cannot tell which number is wrong.
PERCENTAGE_TICK = 0.0001


def get_db_records(
    query_modifier = None,
    table_name: str = 'idx_filings',
    columns: str = '*',
) -> list[dict]:
    query = (
        SUPABASE_CLIENT
        .table(table_name)
        .select(columns)
    )

    if query_modifier is not None:
        query = query_modifier(query)

    response = query.execute()

    return response.data


def get_previous_filing(
    holder_name: str,
    symbol: str,
    timestamp: str,
) -> dict | None:
    records = get_db_records(
        query_modifier=lambda query: (
            query
            .eq("holder_name", holder_name)
            .eq("symbol", symbol)
            .lt("timestamp", timestamp)
            .order("timestamp", desc=True)
            .limit(1)
        )
    )

    return records[0] if records else None


def get_transaction_month(filing_record: dict) -> str | None:
    """
    'YYYY-MM' of the earliest transaction. holding_before is the position before
    that trade, so the report to check is the one covering the month before it -
    which is the one published in this same month.
    """
    dates = [
        transaction.get('date')
        for transaction in filing_record.get('price_transaction') or []
        if transaction.get('date')
    ]

    return min(dates)[:7] if dates else None


def run_ammend(filing_record: dict) -> bool:
    holder_name = filing_record.get('holder_name')
    symbol = filing_record.get('symbol')
    timestamp = filing_record.get('timestamp')
    holding_before = filing_record.get('holding_before')
    holding_after = filing_record.get('holding_after')
    share_pct_before = filing_record.get('share_percentage_before')
    share_pct_after = filing_record.get('share_percentage_after')
    net_shares = filing_record.get('net_shares_transacted')

    # Everything below divides by share_pct_before to get shares outstanding, so a
    # missing one, or a holder opening a position from zero leaves nothing to
    # compute against
    if not share_pct_before:
        LOGGER.info('amend: no share_percentage_before, cannot derive shares outstanding')
        return False

    # share_pct_after of 0.0 is a holder exiting in full. That is a real value, not a
    # missing one, and the arithmetic handles it, only None means we cannot verify
    if share_pct_after is None:
        LOGGER.info('amend: no share_percentage_after, nothing to verify against')
        return False

    # Step 1 - is holding_before trustworthy? Everything below is built on it, so it
    # needs backing from outside this document: the holder's previous filing, or
    # failing that, their position in the monthly securities report
    previous_filing = get_previous_filing(
        holder_name,
        symbol,
        timestamp
    )

    anchor_holding = previous_filing.get('holding_after') if previous_filing else None

    if anchor_holding != holding_before:
        LOGGER.info(
            'amend: no previous filing chains to holding_before %s (previous holding_after: %s), '
            'falling back to the monthly securities report',
            holding_before, anchor_holding
        )

        month = get_transaction_month(filing_record)

        if month is None:
            LOGGER.info('amend: no transaction date, cannot pick a securities report')
            return False

        anchor_holding = get_securities_data(
            symbol=symbol,
            holder_name=holder_name,
            month=month
        )

    if anchor_holding != holding_before:
        LOGGER.info(
            'amend: holding_before %s could not be confirmed (anchor: %s), not amending',
            holding_before, anchor_holding
        )
        return False

    # Step 2 - how fine a change can the reported percentage actually express?
    shares_outstanding = holding_before / (share_pct_before / 100)
    precision = shares_outstanding * PERCENTAGE_TICK

    expected_after = holding_before + net_shares
    mismatch = abs(expected_after - holding_after)

    if mismatch < precision:
        LOGGER.info(
            'amend: mismatch of %s shares is below the percentage precision of %s - '
            'cannot tell whether holding_after is wrong or a row was mis-extracted',
            mismatch, round(precision)
        )
        return False

    implied_after = shares_outstanding * (share_pct_after / 100)

    # Branch A - the percentage backs up the transaction rows, so the rows are
    # complete and holding_after is the number that is wrong
    if abs(expected_after - implied_after) <= precision:
        amended_pct_after = round(expected_after / shares_outstanding * 100, 2)

        filing_record['holding_after'] = expected_after
        filing_record['share_percentage_after'] = amended_pct_after
        filing_record['share_percentage_transaction'] = round(
            abs(amended_pct_after - share_pct_before), 3
        )

        LOGGER.info(
            'amend: holding_after %s -> %s, share_percentage_after %s -> %s',
            holding_after, expected_after, share_pct_after, amended_pct_after
        )
        return True

    # Branch B - the percentage contradicts the rows, so holding_after stands and
    # a transaction row never made it out of the parser. Add it back without a price
    missing_shares = holding_after - holding_before - net_shares

    price_transaction = filing_record.get('price_transaction') or []
    
    price_transaction.append({
        'date': str(timestamp)[:10],
        'type': 'buy' if missing_shares > 0 else 'sell',
        'price': None,
        'amount_transacted': abs(missing_shares),
    })

    filing_record['price_transaction'] = price_transaction

    enrich_transaction(filing_record, 'split')

    LOGGER.info('amend: added missing transaction row of %s shares', missing_shares)
    return True


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    record = {
    "holding_before": 45425300,
    "holding_after": 428253,
    "share_percentage_before": 5.63,
    "share_percentage_after": 5.3,
    "share_percentage_transaction": 0.33,
    "symbol": "NASI.JK",
    "company_name": "Wahana Inti Makmur",
    "holder_name": "Hartarto Ciputra",
    "timestamp": "2026-06-17 20:27:53",
    "source": "doc2.pdf",
    "sector": "consumer-non-cyclicals",
    "sub_sector": "food-beverage",
    "price_transaction": [
      {
        "type": "sell",
        "amount_transacted": 2900000,
        "price": 125,
        "date": "2026-06-08",
        "purpose": "trading",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 300000,
        "price": 114,
        "date": "2026-06-08",
        "purpose": "averaging down",
        "classification": "Saham Biasa"
      }
    ],
    "price": 126.269,
    "transaction_value": 328300000,
    "transaction_type": "sell",
    "net_shares_transacted": -2600000,
    "amount_transaction": 44997047,
    "reasons": [
      "transaction value mismatch: holding_before=45425300 + net_shares=-2600000 = 42825300, but holding_after=428253"
    ]
  }
    
    record2={
    "holding_before": 19647479100,
    "holding_after": 19702501700,
    "share_percentage_before": 35.78,
    "share_percentage_after": 35.88,
    "share_percentage_transaction": 0.1,
    "symbol": "IMPC.JK",
    "company_name": "Impack Pratama Industri",
    "holder_name": "Harimas Tunggal Perkasa",
    "source": "doc2.pdf",
    "sector": "industrials",
    "sub_sector": "industrial-goods",
    "price_transaction": [
      {
        "type": "buy",
        "amount_transacted": 201500,
        "price": 1785,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1815,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1810,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1805,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1760,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1850,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1845,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1840,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1835,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 115800,
        "price": 1830,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1800,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 200000,
        "price": 1770,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 1219600,
        "price": 1880,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 67200,
        "price": 1885,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 7932800,
        "price": 1890,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 257800,
        "price": 1775,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 297200,
        "price": 1790,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1820,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 232200,
        "price": 1780,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1755,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1750,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 129500,
        "price": 1765,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 400,
        "price": 1875,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1825,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 1994400,
        "price": 1795,
        "date": "2026-06-04",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1725,
        "date": "2026-06-05",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1745,
        "date": "2026-06-05",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1705,
        "date": "2026-06-05",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 1198700,
        "price": 1530,
        "date": "2026-06-05",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1720,
        "date": "2026-06-05",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1740,
        "date": "2026-06-05",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1735,
        "date": "2026-06-05",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1710,
        "date": "2026-06-05",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1750,
        "date": "2026-06-05",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1730,
        "date": "2026-06-05",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 100000,
        "price": 1715,
        "date": "2026-06-05",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 300000,
        "price": 1335,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 157800,
        "price": 1445,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 19892100,
        "price": 1450,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 2400,
        "price": 1420,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 92300,
        "price": 1360,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 202500,
        "price": 1430,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 143700,
        "price": 1425,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 3700,
        "price": 1435,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 57400,
        "price": 1440,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 149100,
        "price": 1400,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 58900,
        "price": 1405,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 300000,
        "price": 1330,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 300000,
        "price": 1355,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 300000,
        "price": 1340,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 300000,
        "price": 1345,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 300000,
        "price": 1350,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 300000,
        "price": 1310,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 300000,
        "price": 1320,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 212300,
        "price": 1375,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 213100,
        "price": 1380,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 207700,
        "price": 1385,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 14125500,
        "price": 1305,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 300000,
        "price": 1325,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 300000,
        "price": 1315,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 216000,
        "price": 1390,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 4800,
        "price": 1395,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 29200,
        "price": 1410,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 200000,
        "price": 1365,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 200000,
        "price": 1370,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      },
      {
        "type": "buy",
        "amount_transacted": 7000,
        "price": 1415,
        "date": "2026-06-08",
        "purpose": "Investasi",
        "classification": "Saham Biasa"
      }
    ],
    "price": 1513.794,
    "timestamp": "2026-06-09 20:27:53",
    "transaction_value": 83747034000,
    "transaction_type": "buy",
    "net_shares_transacted": 55322600,
    "amount_transaction": 55022600,
    "reasons": [
      "transaction value mismatch: holding_before=19647479100 + net_shares=55322600 = 19702801700, but holding_after=19702501700"
    ]
  }
    
    
    result = run_ammend(record)
    print(f'\nresult: {result}\n')
    # print(f'new record: {record}')
    # uv run -m idx_pipeline.parser.amend
