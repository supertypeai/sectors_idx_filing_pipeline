from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import logging

from idx_pipeline.utils.helper import get_db


LOGGER = logging.getLogger(__name__)


def get_daily_data(
    symbol: str,
    earliest_date: str,
    latest_date: str,
) -> list[dict]:
    return get_db(
        table="idx_daily_data",
        columns="date, low, high, close",
        query_modifier=lambda query: (
            query
            .eq("symbol", symbol)
            .gte("date", earliest_date)
            .lte("date", latest_date)
            .order("date")
        ),
    )


def get_date_window(
    transaction_date: str,
    market_window_days: int,
) -> tuple[str, str]:
    parsed_date = date.fromisoformat(transaction_date)
    window_start = parsed_date - timedelta(days=market_window_days)
    window_end = parsed_date + timedelta(days=market_window_days)

    return str(window_start), str(window_end)


def parse_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None

    try:
        return Decimal(str(value))

    except (InvalidOperation, ValueError):
        return None


def get_market_price_range(
    daily_data: list[dict],
    transaction_date: str,
    market_window_days: int = 5,
) -> tuple[Decimal, Decimal] | None:
    lowest_prices = []
    highest_prices = []

    window_start, window_end = get_date_window(
        transaction_date=transaction_date,
        market_window_days=market_window_days,
    )

    for daily_record in daily_data:
        market_date = daily_record.get("date")

        if market_date is None or not window_start <= market_date <= window_end:
            continue

        low_price = parse_decimal(daily_record.get("low"))
        high_price = parse_decimal(daily_record.get("high"))

        if low_price is None or high_price is None:
            continue

        # A zero or inverted OHLC row is invalid market data. It must not widen
        # the range and make an unrelated filed price look valid.
        if low_price <= 0 or high_price <= 0 or low_price > high_price:
            continue

        lowest_prices.append(low_price)
        highest_prices.append(high_price)

    if not lowest_prices or not highest_prices:
        return None

    return min(lowest_prices), max(highest_prices)


def normalize_price(price: Decimal) -> int | float:
    if price == price.to_integral_value():
        return int(price)

    return float(price)


def is_price_in_market_range(
    price: Decimal,
    market_price_range: tuple[Decimal, Decimal],
) -> bool:
    lowest_price, highest_price = market_price_range
    return lowest_price <= price <= highest_price


def get_unit_price_candidate(
    reported_price: Decimal,
    amount_transacted: object,
) -> Decimal | None:
    amount = parse_decimal(amount_transacted)

    if amount is None or amount <= 0:
        return None

    candidate_price = reported_price / amount

    return candidate_price


def get_holdings_change(
    holding_before: object,
    holding_after: object,
) -> Decimal | None:
    before = parse_decimal(holding_before)
    after = parse_decimal(holding_after)

    if before is None or after is None or before == after:
        return None

    return abs(after - before)


def validate_and_correct_transaction_prices(
    transactions: list[dict],
    symbol: str,
    market_window_days: int = 5,
    holding_before: object = None,
    holding_after: object = None,
) -> list[str]:
    transaction_dates = [
        date.fromisoformat(transaction["date"])
        for transaction in transactions
        if transaction.get("date")
    ]

    if not transaction_dates:
        return []

    window_start = min(transaction_dates) - timedelta(days=market_window_days)
    latest_market_date = date.today() - timedelta(days=1)

    window_end = min(
        max(transaction_dates) + timedelta(days=market_window_days),
        latest_market_date,
    )

    daily_data = get_daily_data(
        symbol=symbol,
        earliest_date=str(window_start),
        latest_date=str(window_end),
    )

    for transaction in transactions:
        transaction_date = transaction.get("date")
        reported_price = parse_decimal(transaction.get("price"))

        # A missing price is handled by the existing filing checks. There is no
        # reported value here to validate or safely repair
        if not transaction_date or reported_price is None:
            continue

        # Repurchase agreements are validated too: filers put the total value in
        # the price column there just as often as on plain trades
        market_price_range = get_market_price_range(
            daily_data=daily_data,
            transaction_date=transaction_date,
            market_window_days=market_window_days,
        )

        if market_price_range is None:
            LOGGER.warning(
                "[PARSE] %s %s: no market data, price check skipped (filed price kept)",
                symbol,
                transaction_date,
            )
            continue

        if is_price_in_market_range(reported_price, market_price_range):
            continue

        unit_price_candidate = get_unit_price_candidate(
            reported_price=reported_price,
            amount_transacted=transaction.get("amount_transacted"),
        )

        if (
            unit_price_candidate is not None
            and is_price_in_market_range(
                unit_price_candidate, 
                market_price_range
            )
        ):
            transaction["price"] = normalize_price(unit_price_candidate)

            LOGGER.warning(
                "[PARSE] %s %s: price corrected %s → %s (filed value was outside the market range)",
                symbol,
                transaction_date,
                reported_price,
                unit_price_candidate,
            )

            continue

        # Some filers shift the row one column left: the share count lands in
        # "unexercised shares", the price in "Number of Shares" and the total in
        # "Transaction Price". The holdings change is then the only true share
        # count, and it only belongs to this row when the filing has one row
        holdings_change = (
            get_holdings_change(holding_before, holding_after)
            if len(transactions) == 1
            else None
        )

        holdings_price_candidate = (
            get_unit_price_candidate(reported_price, holdings_change)
            if holdings_change is not None
            else None
        )

        if (
            holdings_price_candidate is not None
            and is_price_in_market_range(
                holdings_price_candidate,
                market_price_range
            )
        ):
            filed_amount = transaction.get("amount_transacted")

            transaction["price"] = normalize_price(holdings_price_candidate)
            transaction["amount_transacted"] = int(holdings_change)

            LOGGER.warning(
                "[PARSE] %s %s: price corrected %s → %s and shares %s → %s "
                "(filed share count did not match the holdings change)",
                symbol,
                transaction_date,
                reported_price,
                holdings_price_candidate,
                filed_amount,
                int(holdings_change),
            )

            continue

        LOGGER.warning(
            "[PARSE] %s %s: filed price %s kept, outside market range %s",
            symbol,
            transaction_date,
            reported_price,
            f"{market_price_range[0]}–{market_price_range[1]}",
        )

    return []
