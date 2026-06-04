from statistics import median 
from datetime import datetime, timedelta

from .utils.matching import get_db 

import logging 
import bisect 


LOGGER = logging.getLogger(__name__)


def format_rupiah(value: float) -> str:
    if value >= 1_000_000_000_000:
        return f"Rp {value / 1_000_000_000_000:.1f}T"
    
    if value >= 1_000_000_000:
        return f"Rp {value / 1_000_000_000:.1f}B"
    
    return f"Rp {value / 1_000_000:.1f}M"


def compute_mad_score(
    historical_filings: list[dict],
    current_trade: float,
) -> tuple[float, float, float, int]:
    non_zero_values = [
        record["transaction_value"]
        for record in historical_filings
        if record.get("transaction_value") and record["transaction_value"] != 0
    ]

    if len(non_zero_values) < 6:
        LOGGER.info('Not enough historical data to compute median, length: %s', len(non_zero_values))
        return 0.0, 0.0, len(non_zero_values)

    central_value = median(non_zero_values)

    absolute_deviations = [abs(value - central_value) for value in non_zero_values]
    mad = median(absolute_deviations)

    if mad == 0:
        return 0.0, central_value, len(non_zero_values)

    score = (current_trade - central_value) / mad
    return score, central_value, len(non_zero_values)


def get_nearest_price(
    target_date,
    price_lookup: dict,
    available_dates: list[str]
) -> float | None:
    target_str = str(target_date)
    
    if target_str in price_lookup:
        return price_lookup[target_str]
    
    index = bisect.bisect_left(available_dates, target_str)
    
    if index < len(available_dates):
        return price_lookup[available_dates[index]]
    
    return None


def compute_historical_price_movement(
    client,
    current_symbol: str, 
    current_transaction_type: str,
    current_timestamp: str, 
    db_filings: list[dict]
) -> str | None:
    historical_filings = [
        record
        for record in db_filings
        if record.get('symbol') == current_symbol
        and record.get('transaction_type') == current_transaction_type
        and record.get('timestamp') < current_timestamp
        and (datetime.fromisoformat(record.get('timestamp')).date() + timedelta(days=60)) <= datetime.today().date()
    ]

    if len(historical_filings) < 10:
        LOGGER.info(
            'Not enough historical data to compute price movement: %s',
            len(historical_filings)
        )
        return None 

    filing_dates = [
        datetime.fromisoformat(record.get('timestamp')).date()
        for record in historical_filings
    ]

    min_date = min(filing_dates)
    max_date = max(filing_dates) + timedelta(days=90)

    daily_data = get_db(
        client=client,
        table='idx_daily_data',
        query_modifier=lambda query: query
            .eq('symbol', current_symbol)
            .gte('date', str(min_date))
            .lte('date', str(max_date)),
    )

    price_lookup = {
        record.get('date'): record.get('close')
        for record in daily_data
        if record.get('close') is not None 
    }

    available_dates = sorted(price_lookup.keys())

    returns_by_date = {}
    
    for record in historical_filings:
        filing_date = datetime.fromisoformat(record.get('timestamp')).date()

        if filing_date in returns_by_date:
            continue

        price_at_filing = get_nearest_price(
            filing_date,
            price_lookup, 
            available_dates
        )

        price_60_days_later = get_nearest_price(
            filing_date + timedelta(days=60), 
            price_lookup, 
            available_dates
        )

        if price_at_filing is None or price_60_days_later is None:
            LOGGER.info(
                'Price is missing for filing: %s and price + 60 days: %s', 
                price_at_filing, price_60_days_later
            )
            continue

        if price_at_filing == 0:
            continue

        return_pct = (price_60_days_later - price_at_filing) / price_at_filing * 100
        returns_by_date[filing_date] = return_pct

    returns = list(returns_by_date.values())

    if len(returns) < 8:
        return None

    average_return = sum(returns) / len(returns)
    median_return = median(returns)

    move_label = 'gained' if average_return > 0 else 'declined'
    cleaned_symbol = current_symbol.removesuffix('.JK')

    return (
        f"Insider {current_transaction_type} in {cleaned_symbol} historically "
        f"{move_label} {abs(average_return):.1f}% on average "
        f"(median: {median_return:.1f}%) within 60 days."
    )


def compute_filing_density(
    db_filings: list[dict],
    current_symbol: str,
    current_timestamp: str
) -> str:
    current_date = datetime.strptime(current_timestamp[:10], "%Y-%m-%d").date()
    window_start = current_date - timedelta(days=14)

    distinct_holders = set(
        record.get("holder_name")
        for record in db_filings
        if record.get("symbol") == current_symbol
        and record.get("holder_name")
        and record.get("timestamp") < current_timestamp
        and window_start <= datetime.strptime(record.get("timestamp", "")[:10], "%Y-%m-%d").date() <= current_date
    )

    if len(distinct_holders) < 2: 
        LOGGER.info('Not enough distinct holder for higlight filing density: %s', len(distinct_holders))
        return None 
    
    cleaned_symbol = current_symbol.removesuffix('.JK')

    return f"{len(distinct_holders)} insiders filed on {cleaned_symbol} within 14 days."


def compute_size_trades(
    db_filings: list[dict],
    current_holder_name: str,
    current_timestamp: str,
    current_transaction_value: int,
    current_transaction_type: str
) -> str | None:
    historical_filings = [
        record
        for record in db_filings
        if record.get('holder_name')
        and record.get('holder_name').strip().lower() == current_holder_name.strip().lower()
        and record.get("timestamp") < current_timestamp
    ]

    score, central_value, history_count = compute_mad_score(
        historical_filings=historical_filings,
        current_trade=current_transaction_value
    )

    LOGGER.info('score: %s, central_value: %s, ratio: %s', score, central_value, current_transaction_value / central_value if central_value > 0 else 0)

    ratio = current_transaction_value / central_value if central_value > 0 else 0

    if score < 3 or current_transaction_value < 500_000_000 or ratio < 3:
        return None
    
    signal_size_trade = (
        f"{ratio:.1f}x typical {current_transaction_type} size "
        f"(based on {history_count} prior trades, "
        f"median {format_rupiah(central_value)}), "
        f"at {format_rupiah(current_transaction_value)}"
    )

    return signal_size_trade


def build_highlights(
    client,
    db_filings: list[dict], 
    current_filing: dict[str, any]
) -> list[dict]:
    current_holder_name = current_filing.get('holder_name')
    current_timestamp = current_filing.get('timestamp')
    current_transaction_value = current_filing.get('transaction_value')
    current_transaction_type = current_filing.get('transaction_type')
    current_symbol = current_filing.get('symbol')

    higlights = []
    
    signal_size_trade = compute_size_trades(
        db_filings,
        current_holder_name,
        current_timestamp,
        current_transaction_value, 
        current_transaction_type
    )

    if signal_size_trade:
        higlights.append(signal_size_trade)

    signal_price_movement = compute_historical_price_movement(
        client,
        current_symbol,
        current_transaction_type,
        current_timestamp,
        db_filings
    )

    if signal_price_movement:
        higlights.append(signal_price_movement)

    signal_filing_density = compute_filing_density(
        db_filings, 
        current_symbol,
        current_timestamp
    )

    if signal_filing_density:
        higlights.append(signal_filing_density)

    return higlights

