from idx_pipeline.utils.helper import get_db
from .securities_report import get_securities_data
from .utils.helper import enrich_transaction 
from .audit import HoldingsCheck, number

import logging


LOGGER = logging.getLogger(__name__)

# Share percentages are reported to 2 decimals, so the smallest change the document
# can express is 0.01% of shares outstanding. Any mismatch below that is invisible
# to the percentage, and it cannot tell which number is wrong.
PERCENTAGE_TICK = 0.0001


def get_previous_filing(
    holder_name: str,
    symbol: str,
    timestamp: str,
) -> dict | None:
    records = get_db(
        table="idx_filings",
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


def find_duplicate_transction( 
    price_transactions: list[dict],
) -> list[dict]:
    duplicate_transactions = []
    
    for transaction in price_transactions: 
        if transaction.get("type") not in {"buy", "sell"}:
            continue 

        duplicate_count = sum(
            candidate == transaction 
            for candidate in price_transactions
        )

        if duplicate_count < 2:
            continue

        if transaction not in duplicate_transactions:
            duplicate_transactions.append(transaction)

    return duplicate_transactions


def has_percentages(filing_record: dict, check: HoldingsCheck) -> bool:
    share_pct_before = filing_record.get("share_percentage_before")
    share_pct_after = filing_record.get("share_percentage_after")

    # Everything below divides by share_pct_before to get shares outstanding, so a
    # missing one, or a holder opening a position from zero leaves nothing to
    # compute against
    if not share_pct_before:
        LOGGER.debug("amend: no share_percentage_before, cannot derive shares outstanding")
        check.evidence.append("filing has no share % before, so shares outstanding cannot be derived")
        return False

    # share_pct_after of 0.0 is a holder exiting in full. That is a real value, not a
    # missing one, and the arithmetic handles it, only None means we cannot verify
    if share_pct_after is None:
        LOGGER.debug("amend: no share_percentage_after, nothing to verify against")
        check.evidence.append("filing has no share % after to verify against")
        return False

    return True


def holding_from_previous_filing(filing_record: dict, check: HoldingsCheck) -> int | None:
    holding_before = filing_record.get("holding_before")

    previous_filing = get_previous_filing(
        filing_record.get("holder_name"),
        filing_record.get("symbol"),
        filing_record.get("timestamp"),
    )

    if not previous_filing:
        check.evidence.append("no previous filing for this holder")
        return None

    anchor_holding = previous_filing.get("holding_after")

    # The id lets a reviewer open the exact row the decision was based on
    previous_label = (
        f"previous filing (id {previous_filing.get('id')}, "
        f"{(previous_filing.get('timestamp') or '')[:10]})"
    )

    if anchor_holding == holding_before:
        check.evidence.append(
            f"holding_before {number(holding_before)} matches the {previous_label}"
        )
    else:
        check.evidence.append(
            f"{previous_label} holding_after = {number(anchor_holding)} (no match)"
        )

    return anchor_holding


def holding_from_securities_report(filing_record: dict, check: HoldingsCheck) -> int | None:
    symbol = filing_record.get("symbol")
    holding_before = filing_record.get("holding_before")
    month = get_transaction_month(filing_record)

    if month is None:
        LOGGER.debug("amend: no transaction date, cannot pick a securities report")
        check.evidence.append("no transaction date, so no securities report to check")
        return None

    report = {}

    # The report comes from idx.co.id too. If it cannot be fetched the filing
    # is still usable, it just cannot be confirmed, so this must not end the run
    try:
        anchor_holding = get_securities_data(
            symbol=symbol,
            holder_name=filing_record.get("holder_name"),
            month=month,
            details=report,
        )

    except Exception as error:
        LOGGER.warning("[PARSE] securities report %s for %s could not be fetched: %s", month, symbol, str(error)[:160])
        check.evidence.append(f"securities report {month}: could not be fetched")
        return None

    check.securities_report_url = report.get("url")

    if not report.get("found"):
        check.evidence.append(f"securities report {month}: no report for {symbol}")
    elif anchor_holding is None:
        check.evidence.append(f"securities report {month}: holder not listed")
    else:
        check.evidence.append(
            f"securities report {month}: {number(anchor_holding)} shares "
            f"(matched \"{report.get('matched_name')}\")"
            + ("" if anchor_holding == holding_before else ", no match")
        )

    return anchor_holding


def confirm_holding_before(filing_record: dict, check: HoldingsCheck) -> bool:
    """
    Step 1 - is holding_before trustworthy? Everything below is built on it, so it
    needs backing from outside this document: the holder's previous filing, or
    failing that, their position in the monthly securities report
    """
    holding_before = filing_record.get("holding_before")
    anchor_holding = holding_from_previous_filing(filing_record, check)

    if anchor_holding != holding_before:
        LOGGER.debug(
            "amend: no previous filing chains to holding_before %s (previous holding_after: %s), "
            "falling back to the monthly securities report",
            holding_before, anchor_holding
        )
        anchor_holding = holding_from_securities_report(filing_record, check)

    if anchor_holding != holding_before:
        LOGGER.debug(
            "amend: holding_before %s could not be confirmed (anchor: %s), not amending",
            holding_before, anchor_holding
        )
        check.evidence.append("so holding_before could not be confirmed")
        return False

    return True


def get_share_precision(filing_record: dict) -> tuple[float, float]:
    """
    Step 2 - how fine a change can the reported percentage actually express?
    Returns (shares outstanding, smallest share change the percentage can show).
    """
    holding_before = filing_record.get("holding_before")
    share_pct_before = filing_record.get("share_percentage_before")

    shares_outstanding = holding_before / (share_pct_before / 100)

    return shares_outstanding, shares_outstanding * PERCENTAGE_TICK


def is_mismatch_visible(filing_record: dict, precision: float, check: HoldingsCheck) -> bool:
    expected_after = filing_record.get("holding_before") + filing_record.get("net_shares_transacted")
    mismatch = abs(expected_after - filing_record.get("holding_after"))

    if mismatch >= precision:
        return True

    check.evidence.append(
        f"gap of {number(mismatch)} shares is below what the reported % can show "
        f"(~{number(round(precision))} shares), cannot tell which number is wrong"
    )
    LOGGER.debug(
        "amend: mismatch of %s shares is below the percentage precision of %s - "
        "cannot tell whether holding_after is wrong or a row was mis-extracted",
        mismatch, round(precision)
    )
    return False


def amend_holding_after(
    filing_record: dict,
    shares_outstanding: float,
    precision: float,
    check: HoldingsCheck,
) -> bool:
    """
    Branch A - the percentage backs up the transaction rows, so the rows are
    complete and holding_after is the number that is wrong
    """
    holding_after = filing_record.get("holding_after")
    share_pct_before = filing_record.get("share_percentage_before")
    share_pct_after = filing_record.get("share_percentage_after")

    expected_after = filing_record.get("holding_before") + filing_record.get("net_shares_transacted")
    implied_after = shares_outstanding * (share_pct_after / 100)

    if abs(expected_after - implied_after) > precision:
        return False

    amended_pct_after = round(expected_after / shares_outstanding * 100, 2)

    filing_record["holding_after"] = expected_after
    filing_record["share_percentage_after"] = amended_pct_after
    filing_record["share_percentage_transaction"] = round(
        abs(amended_pct_after - share_pct_before), 3
    )

    LOGGER.debug(
        "amend: holding_after %s -> %s, share_percentage_after %s -> %s",
        holding_after, expected_after, share_pct_after, amended_pct_after
    )

    check.wrong = "holding_after"
    check.evidence.append(
        f"reported share % after ({share_pct_after}%) agrees with the transaction rows"
    )
    check.action = (
        f"holding_after {number(holding_after)} → {number(expected_after)} "
        f"(share % after {share_pct_after} → {amended_pct_after})"
    )
    return True


def find_reconciling_duplicates(filing_record: dict) -> list[dict]:
    """
    Every duplicated row whose removal makes holding_before + net equal the filed
    holding_after, each with the transaction list it would leave behind
    """
    holding_before = filing_record.get("holding_before")
    holding_after = filing_record.get("holding_after")
    net_shares = filing_record.get("net_shares_transacted")
    price_transaction = filing_record.get("price_transaction") or []

    reconciled_candidates = []

    for duplicate_transaction in find_duplicate_transction(price_transactions=price_transaction):
        amount_transacted = duplicate_transaction.get("amount_transacted")

        if amount_transacted is None:
            continue

        if duplicate_transaction.get("type") == "buy":
            removed_net_effect = amount_transacted
        else:
            removed_net_effect = -amount_transacted

        if holding_before + net_shares - removed_net_effect != holding_after:
            continue

        candidate_transactions = price_transaction.copy()
        candidate_transactions.remove(duplicate_transaction)

        reconciled_candidates.append({
            "removed_transaction": duplicate_transaction,
            "price_transactions": candidate_transactions,
        })

    return reconciled_candidates


def remove_duplicate_row(filing_record: dict, check: HoldingsCheck) -> bool:
    """
    Branch B - the percentage contradicts the rows, so holding_after stands and a
    transaction row was extracted twice. Only fixed when exactly one duplicate
    explains the gap
    """
    share_pct_after = filing_record.get("share_percentage_after")
    reconciled_candidates = find_reconciling_duplicates(filing_record)

    check.wrong = "transaction row"

    if len(reconciled_candidates) == 1:
        reconciled_candidate = reconciled_candidates[0]
        removed = reconciled_candidate["removed_transaction"]

        filing_record["price_transaction"] = reconciled_candidate["price_transactions"]
        enrich_transaction(filing_record, "combine")

        LOGGER.debug("amend: removed exact duplicate transaction row %s", removed)

        check.evidence.append(
            f"reported share % after ({share_pct_after}%) contradicts the rows; removing one "
            "exact duplicate row makes the holdings add up"
        )
        check.action = (
            f"removed duplicate row: {removed.get('type')} {number(removed.get('amount_transacted'))} "
            f"@ {removed.get('price')} on {removed.get('date')}"
        )
        return True

    if len(reconciled_candidates) > 1:
        LOGGER.debug(
            "amend: multiple duplicate transaction removals reconcile holdings; "
            "cannot determine which row is incorrect"
        )
        check.evidence.append(
            f"reported share % after ({share_pct_after}%) contradicts the rows, but more than "
            "one duplicate row could be the extra one"
        )
        return False

    LOGGER.debug(
        "amend: transaction rows do not reconcile and no exact duplicate removal "
        "resolves the mismatch"
    )
    check.evidence.append(
        f"reported share % after ({share_pct_after}%) contradicts the rows, and no duplicate "
        "row explains the gap"
    )
    return False


def run_ammend(filing_record: dict, check: HoldingsCheck | None = None) -> bool:
    """
    Tries to repair a filing whose holdings do not add up. Returns True when it
    changed the record. Every decision is also written to check (wrong / evidence /
    action), which is what ends up in the log card and the corrections file.
    """
    if check is None:
        check = HoldingsCheck(None, None, None)

    if not has_percentages(filing_record, check):
        return False

    if not confirm_holding_before(filing_record, check):
        return False

    shares_outstanding, precision = get_share_precision(filing_record)

    if not is_mismatch_visible(filing_record, precision, check):
        return False

    if amend_holding_after(filing_record, shares_outstanding, precision, check):
        return True

    return remove_duplicate_row(filing_record, check)
