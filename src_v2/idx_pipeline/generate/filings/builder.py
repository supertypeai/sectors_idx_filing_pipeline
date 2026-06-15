from datetime import datetime, timedelta
from collections import defaultdict 
from typing import Optional
from pathlib import Path

from idx_pipeline.config.settings import SUPABASE_CLIENT
from .utils.translator import translator
from .utils.matching import matching_investor_and_conglomerates, get_db
from .highlight import build_highlights

import logging 


LOGGER = logging.getLogger(__name__)


def fetch_six_month_history_map(supabase_client, key_lookup: str) -> dict: 
    six_months_ago = datetime.now() - timedelta(days=180)
    six_months_ago_str = six_months_ago.isoformat()

    columns_to_fetch = (
        "symbol, holder_name, transaction_type, amount_transaction, price, "
        "transaction_value, share_percentage_before, share_percentage_after, timestamp"
    )

    response = (
        supabase_client.table("idx_filings")
        .select(columns_to_fetch)
        .gte("timestamp", six_months_ago_str)
        .execute()
    )
    
    historical_records = response.data
    
    history_map = defaultdict(list)
    
    for record in historical_records:
        symbol = record.get(key_lookup)

        if not symbol:
            continue

        history_map[symbol].append(record)
    
    return history_map


def parse_timestamp(timestamp_str: str | None) -> datetime:
    if not timestamp_str:
        return None
    
    return datetime.fromisoformat(timestamp_str.replace('T', ' '))


def format_ordinal(number: int) -> str:
    if not isinstance(number, int):
        return str(number)
        
    if 11 <= (number % 100) <= 13:
        return f"{number}th"
        
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def generate_title(holder_name: str, company_name: str, tx_type: str) -> str:
    action_title = tx_type.replace("-", " ").title()

    title_map = {
        "buy": f"{holder_name} buys shares of {company_name}",
        "sell": f"{holder_name} sells shares of {company_name}",
        "share-transfer": f"{holder_name} transfers shares of {company_name}",
        "award": f"{holder_name} was awarded shares of {company_name}",
        "inheritance": f"{holder_name} inherits shares of {company_name}",
        "others": f"Change in {holder_name}'s position in {company_name}",
    }

    return title_map.get(tx_type, f"{holder_name} {action_title} transaction of {company_name}")


def generate_base_body(raw_dict: dict, purpose_en: str) -> str:
    holder_name = raw_dict.get("holder_name") or "Unknown Shareholder"
    company_name = raw_dict.get("company_name") or "Unknown Company"

    transaction_type = raw_dict.get("transaction_type", "")
    amount_transaction = raw_dict.get("amount_transaction")
    holding_before = raw_dict.get("holding_before")
    holding_after = raw_dict.get("holding_after")

    action_verb_map = {
        "buy": "bought",
        "sell": "sold",
        "share-transfer": "transferred",
        "award": "was awarded",
        "inheritance": "inherited",
        "others": "executed a transaction for",
    }
    action_verb = action_verb_map.get(transaction_type, "executed a transaction for")

    amount_str = f"{amount_transaction:,} shares" if amount_transaction is not None else "shares"
    body = f"{holder_name} {action_verb} {amount_str} of {company_name}."

    if holding_before is not None and holding_after is not None:
        holding_before_str = f"{holding_before:,}"
        holding_after_str = f"{holding_after:,}"

        if holding_after > holding_before:
            body += f" This increases their holdings from {holding_before_str} to {holding_after_str} shares."

        elif holding_after < holding_before:
            body += f" This decreases their holdings from {holding_before_str} to {holding_after_str} shares."

        else:
            body += f" Their holdings remain at {holding_after_str} shares."

    if purpose_en:
        body += f" The stated purpose of the transaction was {purpose_en.lower()}."

    return body


def generate_base_template(
    holder_name: str,
    company_name: str,
    tx_type: str,
    amount: Optional[int],
    holding_before: Optional[int],
    holding_after: Optional[int],
    purpose_en: str,
) -> tuple[str, str]:
    title = generate_title(
        holder_name, 
        company_name, 
        tx_type
    )
    
    body = generate_base_body(
        holder_name, 
        company_name, 
        tx_type, 
        amount, 
        holding_before, 
        holding_after, 
        purpose_en
    )

    return title, body


def generate_cluster_template(
    holder_name: str, 
    transaction_type: str, 
    other_holder_names: list[str], 
    company_name: str, 
    cluster_total_shares: int, 
    cluster_avg_price: float
) -> str:
    volume_noun = "accumulation" if transaction_type == 'buy' else "distribution"
    volume_article = "an" if transaction_type == 'buy' else "a"
    action_noun = "purchase" if transaction_type == "buy" else "disposal"

    if len(other_holder_names) >= 5:
        listed_names = ", ".join(other_holder_names[:4])
        remaining_count = len(other_holder_names) - 4
        formatted_names = f"{listed_names} and {remaining_count} other insiders"

    elif len(other_holder_names) >= 2:
        formatted_names = ", ".join(other_holder_names[:-1]) + f" and {other_holder_names[-1]}"
        
    else:
        formatted_names = other_holder_names[0]

    body = (
        f"This transaction is part of a cluster-{transaction_type} transaction amidst other "
        f"insider {transaction_type}s during the last 6 months. {holder_name}'s insider "
        f"{action_noun} comes after {formatted_names} in {company_name}, "
        f"totaling {volume_article} {volume_noun} of {cluster_total_shares:,} shares transacted at an average "
        f"price of IDR {cluster_avg_price:,.0f}."
    )

    return body


def generate_chain_template(
    holder_name: str, 
    transaction_count: int, 
    transaction_type: str, 
    total_shares: int, 
    weighted_avg_price: float, 
    start_percentage: float, 
    current_percentage: float,
    company_name: str
) -> str:
    # substantial_threshold = 0.05
    # percentage_delta = abs(current_percentage - start_percentage)

    volume_noun = "accumulation" if transaction_type == 'buy' else "distribution"
    volume_article = "an" if transaction_type == 'buy' else "a"
    action_noun = "purchase" if transaction_type == "buy" else "disposal"

    start_percentage_display = round(start_percentage, 3)
    current_percentage_display = round(current_percentage, 3)

    # if percentage_delta < substantial_threshold:
    #     percentage_text = "not substantially changed"

    if current_percentage > start_percentage:
        percentage_text = f"increased from {start_percentage_display}% to {current_percentage_display}%"

    else:
        percentage_text = f"decreased from {start_percentage_display}% to {current_percentage_display}%"

    body = (
        f"This is {holder_name}'s {format_ordinal(transaction_count)} insider {action_noun} "
        f"in the last 6 months, totaling {volume_article} {volume_noun} of {total_shares:,} shares "
        f"transacted at an average price of IDR {weighted_avg_price:,.0f}. "
        f"{holder_name}'s ownership in {company_name} has {percentage_text} in this period."
    )

    return body


def generate_cross_stock_template(
    holder_name: str,
    holder_type: str,
    transaction_type: str,
    current_symbol: str,
    other_symbols: list[str],
    total_transaction_value: int,
) -> str:
    total_company_count = len(other_symbols) + 1
    subject_pronoun = "the firm" if holder_type == 'institution' else holder_name

    if transaction_type == 'buy':
        opening = (
            f"{holder_name}'s acquisition of {current_symbol} shares is the latest in "
            f"a series of investments, with {subject_pronoun} adding to their position across "
            f"{total_company_count} companies in the last 6 months."
        )
        alongside_verb = "acquired"
        period_noun = "purchases"

    else:
        opening = (
            f"{holder_name}'s disposal of {current_symbol} shares is part of a larger "
            f"exposure reduction that sees {subject_pronoun} trimming position across "
            f"{total_company_count} companies in the last 6 months."
        )
        alongside_verb = "disposed"
        period_noun = "disposals"

    if len(other_symbols) >= 4:
        listed_symbols = ", ".join(other_symbols[:4])
        remaining_count = len(other_symbols) - 4
        formatted_symbols = f"{listed_symbols} and {remaining_count} other companies"

    elif len(other_symbols) == 1:
        formatted_symbols = other_symbols[0]
    
    else:
        formatted_symbols = ", ".join(other_symbols[:-1]) + f" and {other_symbols[-1]}"

    alongside = (
        f"Alongside {current_symbol}, {subject_pronoun} has also {alongside_verb} shares of "
        f"{formatted_symbols}, totaling IDR {total_transaction_value:,} across all "
        f"{period_noun} in this period."
    )

    return f"{opening} {alongside}"


def filter_data_for_template(
    filings_based_symbol: list[dict],
    filings_based_holder_name: list[dict], 
    holder_name: str, 
    symbol: str, 
    transaction_type: str,
    current_timestamp: str
):
    current_dt = parse_timestamp(current_timestamp)
    six_months_before_current_dt = current_dt - timedelta(days=180)

    buy_transactions = [
        transaction for transaction in filings_based_symbol
        if transaction.get('holder_name') != holder_name
        and transaction.get('transaction_type') == 'buy'
        and parse_timestamp(transaction.get('timestamp', '')) < current_dt
        and parse_timestamp(transaction.get('timestamp', '')) >= six_months_before_current_dt
    ]

    sell_transactions = [
        transaction for transaction in filings_based_symbol
        if transaction.get('holder_name') != holder_name
        and transaction.get('transaction_type') == 'sell'
        and parse_timestamp(transaction.get('timestamp', '')) < current_dt
        and parse_timestamp(transaction.get('timestamp', '')) >= six_months_before_current_dt
    ]

    repeated_holder_transactions = [
        transaction for transaction in filings_based_symbol
        if transaction.get('holder_name') == holder_name
        and transaction.get('transaction_type') == transaction_type
        and parse_timestamp(transaction.get('timestamp', '')) < current_dt
        and parse_timestamp(transaction.get('timestamp', '')) >= six_months_before_current_dt
    ]

    cross_stock_transactions = [
        transaction for transaction in filings_based_holder_name
        if transaction.get('symbol') != symbol
        and transaction.get('transaction_type') == transaction_type
        and parse_timestamp(transaction.get('timestamp', '')) < current_dt
        and parse_timestamp(transaction.get('timestamp', '')) >= six_months_before_current_dt
    ]

    return buy_transactions, sell_transactions, repeated_holder_transactions, cross_stock_transactions


def route_cluster_template(
    current_filing: dict,
    buy_transactions: list[dict],
    sell_transactions: list[dict],
    repeated_holder_transactions: list[dict],
    holder_name: str,
    company_name: str,
    transaction_type: str,
    symbol: str
) -> tuple[str, str] | None:
    distinct_buy_holder_count = len(set(
        transaction.get('holder_name') for transaction in buy_transactions
    ))
    distinct_sell_holder_count = len(set(
        transaction.get('holder_name') for transaction in sell_transactions
    ))

    buy_sell_difference = distinct_buy_holder_count - distinct_sell_holder_count
    sell_buy_difference = distinct_sell_holder_count - distinct_buy_holder_count

    is_cluster_buy = (
        transaction_type == 'buy'
        and distinct_buy_holder_count >= 2
        and buy_sell_difference >= 2
    )
    is_cluster_sell = (
        transaction_type == 'sell'
        and distinct_sell_holder_count >= 2
        and sell_buy_difference >= 2
    )

    if not is_cluster_buy and not is_cluster_sell:
        return None

    cluster_transactions = buy_transactions if is_cluster_buy else sell_transactions
    all_cluster_transactions = cluster_transactions + repeated_holder_transactions + [current_filing]

    cluster_total_shares = sum(
        transaction.get('amount_transaction', 0)
        for transaction in all_cluster_transactions
    )
    cluster_total_value = sum(
        transaction.get('transaction_value', 0)
        for transaction in all_cluster_transactions
    )
    cluster_avg_price = (
        cluster_total_value / cluster_total_shares
        if cluster_total_shares > 0 else 0.0
    )

    cluster_transactions_sorted = sorted(
        cluster_transactions,
        key=lambda transaction: transaction.get('timestamp', ''),
    )

    other_holder_names = list(dict.fromkeys(
        transaction.get('holder_name')
        for transaction in cluster_transactions_sorted
    ))

    LOGGER.info(f"cluster current holder names: {holder_name}")
    LOGGER.info(f"cluster other holder names: {other_holder_names}")
    LOGGER.info(f"distinct buy holder count: {distinct_buy_holder_count}")

    body = generate_cluster_template(
        holder_name=holder_name,
        transaction_type=transaction_type,
        other_holder_names=other_holder_names,
        company_name=company_name,
        cluster_total_shares=cluster_total_shares,
        cluster_avg_price=cluster_avg_price,
    )

    total_distinct_holder_count = len(set(
        transaction.get('holder_name') for transaction in all_cluster_transactions
    ))

    transaction_verb = "bought" if is_cluster_buy else "sold"
    cleaned_symbol = symbol.strip().removesuffix('.JK')
    context = f"{total_distinct_holder_count} insiders {transaction_verb} {cleaned_symbol} in the last 6 months."

    return body, context


def route_chain_template(
    current_filing: dict,
    repeated_holder_transactions: list[dict],
    holder_name: str,
    company_name: str,
    transaction_type: str,
    start_percentage: float,
    current_percentage: float,
) -> tuple[str, str] | None:
    if len(repeated_holder_transactions) < 2:
        return None

    all_chain_transactions = repeated_holder_transactions + [current_filing]

    chain_total_shares = sum(
        transaction.get('amount_transaction', 0)
        for transaction in all_chain_transactions
    )
    chain_total_value = sum(
        transaction.get('transaction_value', 0)
        for transaction in all_chain_transactions
    )
    chain_avg_price = (
        chain_total_value / chain_total_shares
        if chain_total_shares > 0 else 0.0
    )
    transaction_count = len(all_chain_transactions)

    body =  generate_chain_template(
        holder_name=holder_name,
        transaction_count=transaction_count,
        transaction_type=transaction_type,
        total_shares=chain_total_shares,
        weighted_avg_price=chain_avg_price,
        start_percentage=start_percentage,
        current_percentage=current_percentage,
        company_name=company_name,
    )

    action_noun = "buy" if transaction_type == 'buy' else "sell"
    context = f"{format_ordinal(transaction_count)} insider {action_noun} by {holder_name} in the last 6 months."

    return body, context


def route_cross_stock_template(
    cross_stock_transactions: list[dict],
    holder_name: str,
    holder_type: str,
    transaction_type: str,
    current_symbol: str,
    current_value: int,
) -> tuple[str, str] | None:
    cross_stock_transactions_sorted = sorted(
        cross_stock_transactions,
        key=lambda transaction: transaction.get('timestamp', ''),
        reverse=True,
    )

    distinct_cross_symbols = list(set(
        transaction.get('symbol')
        for transaction in cross_stock_transactions_sorted
    ))
    
    if len(distinct_cross_symbols) < 1:
        return None

    cross_stock_total_value = sum(
        transaction.get('transaction_value', 0)
        for transaction in cross_stock_transactions
    ) + int(current_value)

    current_symbol = current_symbol.strip().removesuffix('.JK')
    distinct_cross_symbols = [symbol.strip().removesuffix('.JK') for symbol in distinct_cross_symbols]

    body = generate_cross_stock_template(
        holder_name=holder_name,
        holder_type=holder_type,
        transaction_type=transaction_type,
        current_symbol=current_symbol,
        other_symbols=distinct_cross_symbols,
        total_transaction_value=cross_stock_total_value,
    )

    all_symbols = [current_symbol] + distinct_cross_symbols
    total_symbol_count = len(all_symbols)
    transaction_verb = "bought" if transaction_type == 'buy' else "sold"

    if total_symbol_count > 2:
        listed_symbols = ", ".join(all_symbols[:2])
        remaining_count = total_symbol_count - 2
        formatted_symbols = f"{listed_symbols} and {remaining_count} other {'company' if remaining_count == 1 else 'companies'}"
    
    else:
        formatted_symbols = " and ".join(all_symbols)

    context = f"{holder_name} {transaction_verb} {formatted_symbols} in the last 6 months."

    return body, context


def route_body_template(
    current_filing: dict,
    filings_based_symbol: list[dict],
    filings_based_holder_name: list[dict],
) -> tuple[str, dict, str | None]:
    symbol = current_filing.get('symbol', '')
    holder_name = current_filing.get('holder_name', '')
    holder_type = current_filing.get('holder_type', '')
    company_name = current_filing.get('company_name', '')
    current_timestamp = current_filing.get('timestamp')
    transaction_type = current_filing.get('transaction_type', '')
    current_value = current_filing.get('transaction_value', 0)
    start_percentage = current_filing.get('share_percentage_before', 0.0)
    current_percentage = current_filing.get('share_percentage_after', 0.0)
    purpose = current_filing.get('purpose', '')

    if transaction_type not in ['buy', 'sell']:
        translated_purpose = translator(purpose)
        body = generate_base_body(
            raw_dict=current_filing,
            purpose_en=translated_purpose
        )
        return body, {"type": "base", "transactions": []}, None

    buy_transactions, sell_transactions, repeated_holder_transactions, cross_stock_transactions = (
        filter_data_for_template(
            filings_based_symbol=filings_based_symbol,
            filings_based_holder_name=filings_based_holder_name,
            holder_name=holder_name,
            symbol=symbol,
            transaction_type=transaction_type,
            current_timestamp=current_timestamp
        )
    )

    LOGGER.info(f'data curent holder: {holder_name}, current symbol: {symbol}, current transaction type: {transaction_type}')
    LOGGER.info(f'data buy cluster: {len(buy_transactions)} for symbol: {symbol}')
    LOGGER.info(f'data sell cluster: {len(sell_transactions)} for symbol: {symbol}')
    LOGGER.info(f'chain: {len(repeated_holder_transactions)} for symbol: {symbol}')
    LOGGER.info(f'cross stock: {len(cross_stock_transactions)} for symbol: {symbol}')

    cluster_result = route_cluster_template(
        current_filing=current_filing,
        buy_transactions=buy_transactions,
        sell_transactions=sell_transactions,
        repeated_holder_transactions=repeated_holder_transactions,
        holder_name=holder_name,
        company_name=company_name,
        transaction_type=transaction_type,
        symbol=symbol,
    )

    if cluster_result is not None:
        cluster_body, cluster_context = cluster_result
        used_transactions = buy_transactions if transaction_type == 'buy' else sell_transactions
        return cluster_body, {"type": "cluster", "transactions": used_transactions}, cluster_context

    chain_result = route_chain_template(
        current_filing=current_filing,
        repeated_holder_transactions=repeated_holder_transactions,
        holder_name=holder_name,
        company_name=company_name,
        transaction_type=transaction_type,
        start_percentage=start_percentage,
        current_percentage=current_percentage,
    )

    if chain_result is not None:
        chain_body, chain_context = chain_result
        return chain_body, {"type": "chain", "transactions": repeated_holder_transactions}, chain_context

    cross_stock_result = route_cross_stock_template(
        cross_stock_transactions=cross_stock_transactions,
        holder_name=holder_name,
        holder_type=holder_type,
        transaction_type=transaction_type,
        current_symbol=symbol,
        current_value=current_value,
    )

    if cross_stock_result is not None:
        cross_stock_body, cross_stock_context = cross_stock_result
        return cross_stock_body, {"type": "cross_stock", "transactions": cross_stock_transactions}, cross_stock_context

    translated_purpose = translator(purpose)
    body = generate_base_body(raw_dict=current_filing, purpose_en=translated_purpose)
    return body, {"type": "base", "transactions": []}, None


def build_title_and_body(
    raw_payload: dict[str, any],
    filing_symbol_map: dict, 
    filing_holder_name_map: dict
):
    symbol = raw_payload.get('symbol')
    holder_name = raw_payload.get('holder_name')
    company_name = raw_payload.get('company_name')
    tx_type = raw_payload.get('transaction_type')

    filing_based_symbol = filing_symbol_map.get(symbol, [])
    filing_based_holder_name = filing_holder_name_map.get(holder_name, [])

    title = generate_title(
        holder_name=holder_name,
        company_name=company_name,
        tx_type=tx_type
    )

    body, context_data, context_str = route_body_template(
        current_filing=raw_payload, 
        filings_based_symbol=filing_based_symbol, 
        filings_based_holder_name=filing_based_holder_name
    )

    raw_payload['title'] = title 
    raw_payload['body'] = body 
    raw_payload['context_data'] = context_data 
    raw_payload['context'] = context_str

    return raw_payload


def enrich(payload: list[dict]): 
    payload_results = []

    filing_symbol_lookup = fetch_six_month_history_map(SUPABASE_CLIENT, 'symbol')
    filing_holder_name_lookup = fetch_six_month_history_map(SUPABASE_CLIENT, 'holder_name')

    idx_investor = get_db(SUPABASE_CLIENT, 'people')
    idx_conglomerates = get_db(SUPABASE_CLIENT, 'conglomerates')
    idx_filings = get_db(SUPABASE_CLIENT, 'idx_filings')

    payload_sorted = sorted(
        payload,
        key=lambda record: record.get('timestamp', '')
    )
    
    for record in payload_sorted: 
        if not isinstance(record, dict):
            continue

        try:
            result = build_title_and_body(
                raw_payload=record,
                filing_symbol_map=filing_symbol_lookup, 
                filing_holder_name_map=filing_holder_name_lookup
            )

            result = matching_investor_and_conglomerates(
                filing=result,
                idx_investor=idx_investor, 
                idx_conglomerates=idx_conglomerates
            )

            result['source_is_manual'] = False

            symbol = record.get('symbol')
            holder_name = record.get('holder_name')

            if symbol:
                filing_symbol_lookup[symbol].append(record)

            if holder_name:
                filing_holder_name_lookup[holder_name].append(record)

            idx_filings.append(record)

            highlights = build_highlights(
                client=SUPABASE_CLIENT, 
                db_filings=idx_filings, 
                current_filing=record
            )

            record['highlights'] = highlights if highlights else None 

            payload_results.append(result)

        except Exception as error:
            LOGGER.error(f"Failed generate filings: {error}. Row: {record}", exc_info=True)
            return []
        
    return payload_results 