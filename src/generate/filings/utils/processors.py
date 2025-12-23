# src/generate/filings/utils/processors.py
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from statistics import median
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from src.core.types import FilingRecord, PriceTransaction, floor_pct_3

try:
    from src.config.config import (
        WITHIN_DOC_RATIO_LOW,
        WITHIN_DOC_RATIO_HIGH,
        MARKET_REF_N_DAYS,
        MARKET_RATIO_LOW,
        MARKET_RATIO_HIGH,
        ZERO_MISSING_X10_MIN,
        ZERO_MISSING_X10_MAX,
        ZERO_MISSING_X100_MIN,
        ZERO_MISSING_X100_MAX,
        PERCENT_TOL_PP,
        GATE_REASONS,
        PRICE_LOOKBACK_DAYS,
    )
except Exception:
    try:
        from config import (  # type: ignore
            WITHIN_DOC_RATIO_LOW, WITHIN_DOC_RATIO_HIGH, MARKET_REF_N_DAYS,
            MARKET_RATIO_LOW, MARKET_RATIO_HIGH, ZERO_MISSING_X10_MIN,
            ZERO_MISSING_X10_MAX, ZERO_MISSING_X100_MIN, ZERO_MISSING_X100_MAX,
            PERCENT_TOL_PP, GATE_REASONS, PRICE_LOOKBACK_DAYS,
        )
    except Exception:
        WITHIN_DOC_RATIO_LOW = 0.5
        WITHIN_DOC_RATIO_HIGH = 1.5
        MARKET_REF_N_DAYS = 20
        MARKET_RATIO_LOW = 0.6
        MARKET_RATIO_HIGH = 1.4
        ZERO_MISSING_X10_MIN = 8.0
        ZERO_MISSING_X10_MAX = 15.0
        ZERO_MISSING_X100_MIN = 80.0
        ZERO_MISSING_X100_MAX = 150.0
        PERCENT_TOL_PP = 0.25
        GATE_REASONS = set()
        PRICE_LOOKBACK_DAYS = 14

try:
    from .provider import (
        get_market_reference,
        suggest_price_range,   
        build_announcement_block,
    )
except Exception:
    from provider import (  # type: ignore
        get_market_reference, suggest_price_range, build_announcement_block,
    )

logger = logging.getLogger(__name__)

# Direction sanity
# def _validate_tx_direction(
#     before: Optional[float],
#     after: Optional[float],
#     tx_type: str,
#     eps: float = 1e-3
# ) -> Tuple[bool, Optional[str]]:
#     try:
#         b = float(before) if before is not None else None
#         a = float(after) if after is not None else None
#     except Exception:
#         return False, "non_numeric_before_after"
#     if b is None or a is None:
#         return False, "missing_before_or_after"

#     t = (tx_type or "").strip().lower()
#     if t == "buy" and a + eps < b:
#         return False, f"inconsistent_buy: after({a}) < before({b})"
#     if t == "sell" and a > b + eps:
#         return False, f"inconsistent_sell: after({a}) > before({b})"
#     return True, None


def _validate_tx_direction(
    before: Optional[float],
    after: Optional[float],
    tx_type: str,
    eps: float = 1e-3
) -> Tuple[bool, Optional[str]]:
    try:
        b = float(before) if before is not None else None
        a = float(after) if after is not None else None
    except Exception:
        return False, "non_numeric_before_after"
    if b is None or a is None:
        return False, "missing_before_or_after"

    t = (tx_type or "").strip().lower()
    if t == "buy" and a + eps < b:
        return False, f"inconsistent_buy: after({a}) < before({b})"
    if t == "sell" and a > b + eps:
        return False, f"inconsistent_sell: after({a}) > before({b})"
    return True, None


def _safe_float(x: Any) -> Optional[float]:
    """Coerce to float; return None if not parseable/empty."""
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None

def _safe_int(x: Any) -> Optional[int]:
    """Coerce to int; return None if not parseable/empty."""
    try:
        if x is None or x == "":
            return None
        return int(x)
    except Exception:
        return None

def _tx_direction_sign(tx_type: Optional[str]) -> int:
    """
    Map transaction type to a signed direction:
      buy  -> +1
      sell -> -1
      other/transfer/unknown -> 0
    """
    t = (tx_type or "").strip().lower()
    if t == "buy":
        return +1
    if t == "sell":
        return -1
    return 0

def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Compute a/b with guards; return None for invalid inputs."""
    if a is None or b is None:
        return None
    if b == 0:
        return None
    try:
        return float(a) / float(b)
    except Exception:
        return None

# Downloads/meta resolver (to attach announcement links/metadata)
def _basename(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        return Path(str(s)).name
    except Exception:
        return str(s)

def _stem(s: Optional[str]) -> Optional[str]:
    b = _basename(s)
    if not b:
        return None
    try:
        return Path(b).stem
    except Exception:
        return b

def _pick_first(*vals):
    """Return the first non-empty value among vals."""
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None

def _resolve_doc_meta(
    record: FilingRecord,
    downloads_meta_map: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Try to retrieve a metadata block for the document (used to build an
    'announcement' section for alerts):
      1) record.source (often a URL)
      2) basename(source), stem(source)
      3) record.raw_data candidates: source | pdf_url | original_url | url | filename | file
         (each tried as exact, basename, and stem)
    """
    if not downloads_meta_map:
        return None

    candidates: List[str] = []
    candidates.append(_pick_first(getattr(record, "source", None), record.raw_data.get("source")))
    for k in ("pdf_url", "original_url", "url", "filename", "file"):
        v = record.raw_data.get(k)
        if v:
            candidates.append(v)

    # Deduplicate while preserving order
    seen = set()
    keys: List[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            keys.append(c)

    # Try exact -> basename -> stem
    for key in keys:
        for probe in (key, _basename(key), _stem(key)):
            if not probe:
                continue
            # Fast path
            if probe in downloads_meta_map:
                it = downloads_meta_map[probe]
                if isinstance(it, dict):
                    return it
            # Loose scan fallback
            for mk, mv in downloads_meta_map.items():
                if mk == probe or _basename(mk) == probe or _stem(mk) == probe:
                    if isinstance(mv, dict):
                        return mv

    return None

# PriceTransaction normalization
# Shape A: List[PriceTransaction]
# Shape B: Single dict with vector fields ({date,type,price,amount_transacted})
# Shape C: List[dict] with "transaction_*" keys (or short aliases)
def _is_db_collapsed_block(obj: Any) -> bool:
    return isinstance(obj, dict) and all(k in obj for k in ("date", "type", "price", "amount_transacted"))

def _expand_db_collapsed_block(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Expand a collapsed block (Shape B) into a list of per-transaction dicts."""
    dates = block.get("date", []) or []
    types = block.get("type", []) or []
    prices = block.get("price", []) or []
    amts = block.get("amount_transacted", []) or []

    n = max(len(dates), len(types), len(prices), len(amts))
    out: List[Dict[str, Any]] = []
    for i in range(n):
        out.append({
            "transaction_date": (dates[i] if i < len(dates) else None),
            "transaction_type": (types[i] if i < len(types) else None),
            "transaction_price": (prices[i] if i < len(prices) else None),
            "transaction_share_amount": (amts[i] if i < len(amts) else None),
        })
    return out

def _normalize_price_tx_list(maybe_list: Any) -> List[Any]:
    """
    Normalize to a homogeneous list of per-transaction dicts/objects for local calculations.
    Does not mutate the original record.
    """
    if not isinstance(maybe_list, list) or not maybe_list:
        return []
    first = maybe_list[0]
    if isinstance(first, dict) and len(maybe_list) == 1 and _is_db_collapsed_block(first):
        return _expand_db_collapsed_block(first)
    return maybe_list

def _tx_get_type(tx: Any) -> Optional[str]:
    if isinstance(tx, PriceTransaction):
        return tx.transaction_type
    if isinstance(tx, dict):
        return tx.get("transaction_type") or tx.get("type")
    return None

def _tx_get_price(tx: Any) -> Optional[float]:
    if isinstance(tx, PriceTransaction):
        return tx.transaction_price
    if isinstance(tx, dict):
        return _safe_float(tx.get("transaction_price") or tx.get("price"))
    return None

def _tx_get_amount(tx: Any) -> Optional[int]:
    if isinstance(tx, PriceTransaction):
        return tx.transaction_share_amount
    if isinstance(tx, dict):
        return _safe_int(
            tx.get("transaction_share_amount")
            or tx.get("amount")
            or tx.get("amount_transacted")
        )
    return None

# Inference helpers
def _infer_total_shares(
    holding_before: Optional[float],
    pp_before: Optional[float],
    holding_after: Optional[float],
    pp_after: Optional[float],
) -> Optional[float]:
    """
    Infer total_shares from (holding, percentage), preferring the 'before' pair.
    """
    hb = _safe_float(holding_before)
    pb = _safe_float(pp_before)
    ha = _safe_float(holding_after)
    pa = _safe_float(pp_after)

    if hb is not None and pb is not None and pb > 0:
        return hb / (pb / 100.0)
    if ha is not None and pa is not None and pa > 0:
        return ha / (pa / 100.0)
    return None

def _compute_document_median_price(prices: List[Union[int, float]]) -> Optional[float]:
    vals = [float(x) for x in prices if isinstance(x, (int, float)) and x > 0]
    if not vals:
        return None
    return float(median(vals))

def _is_x10_or_x100(a: Optional[float], ref: Optional[float]) -> Tuple[bool, Optional[str]]:
    """
    Detect potential missing-zero cases (x10/x100) comparing price to a reference.
    """
    r = _ratio(a, ref)
    if r is None:
        return (False, None)
    rr = abs(r)
    if ZERO_MISSING_X10_MIN <= rr <= ZERO_MISSING_X10_MAX:
        return (True, "x10_candidate")
    if ZERO_MISSING_X100_MIN <= rr <= ZERO_MISSING_X100_MAX:
        return (True, "x100_candidate")
    return (False, None)

# P0-4: Suspicious transaction price detection
def _check_tx_price_outlier(
    tx_price: Optional[float],
    doc_median: Optional[float],
    market_ref: Optional[Dict[str, Any]],
) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Check a single transaction price against:
      1) within-document median
      2) market reference (VWAP/median close over N days)
      3) magnitude anomalies (possible missing zero)
    Returns (suspicious: bool, reasons: List[reason-dicts])
    """
    reasons: List[Dict[str, Any]] = []
    price = _safe_float(tx_price)
    if price is None or price <= 0:
        return (False, reasons)

    suspicious = False

    # 1) Deviation vs document median
    if doc_median:
        r_doc = _ratio(price, doc_median)
        if r_doc is not None and (r_doc < WITHIN_DOC_RATIO_LOW or r_doc > WITHIN_DOC_RATIO_HIGH):
            suspicious = True
            reasons.append({
                "scope": "tx",
                "code": "price_deviation_within_doc",
                "message": f"Price deviates from doc-median by ratio {r_doc:.3f}",
                "details": {"price": price, "doc_median_price": doc_median, "ratio": r_doc},
            })

    # 2) Deviation vs market reference
    market_ref_price = _safe_float((market_ref or {}).get("ref_price"))
    if market_ref_price:
        r_market = _ratio(price, market_ref_price)
        if r_market is not None and (r_market < MARKET_RATIO_LOW or r_market > MARKET_RATIO_HIGH):
            suspicious = True
            reasons.append({
                "scope": "tx",
                "code": "price_deviation_vs_market",
                "message": f"Price deviates from market-ref by ratio {r_market:.3f}",
                "details": {"price": price, "market_ref": market_ref, "ratio": r_market},
            })

        # 3) Magnitude anomaly vs market
        zflag, zlabel = _is_x10_or_x100(price, market_ref_price)
        if zflag:
            suspicious = True
            reasons.append({
                "scope": "tx",
                "code": "possible_zero_missing",
                "message": f"Price magnitude anomaly vs market-ref ({zlabel})",
                "details": {"price": price, "market_ref": market_ref, "ratio": float(f"{r_market:.3f}")},
            })

    # If we don't have market data, still check magnitude vs doc median
    elif doc_median:
        r_doc = _ratio(price, doc_median)
        zflag, zlabel = _is_x10_or_x100(price, doc_median)
        if zflag:
            suspicious = True
            reasons.append({
                "scope": "tx",
                "code": "possible_zero_missing",
                "message": f"Price magnitude anomaly vs doc-median ({zlabel})",
                "details": {"price": price, "doc_median_price": doc_median, "ratio": float(f"{r_doc:.3f}")},
            })

    return (suspicious, reasons)

# Recompute ownership percentages
def _recompute_percentages_model(record: FilingRecord) -> Dict[str, Any]:
    """
    Recompute delta percentage from transactions and compare to PDF-reported
    percentages. Writes results into record.audit_flags.
    """
    share_pct_before_pdf = record.share_percentage_before
    share_pct_after_pdf = record.share_percentage_after
    holding_before = record.holding_before
    holding_after = record.holding_after

    # Prefer parser-provided total_shares when available
    total_shares = _safe_float(record.raw_data.get("total_shares"))
    if total_shares is None:
        total_shares = _infer_total_shares(
            holding_before, share_pct_before_pdf,
            holding_after, share_pct_after_pdf,
        )

    tx_list = _normalize_price_tx_list(record.price_transaction)

    # Net signed amount (+ for buy, − for sell)
    signed_amount = 0.0
    for tx in tx_list:
        amt = _safe_float(_tx_get_amount(tx)) or 0.0
        sgn = _tx_direction_sign(_tx_get_type(tx))
        signed_amount += sgn * amt

    delta_pp_model = None
    pp_after_model = None
    percent_discrepancy = False
    discrepancy_pp = None

    if total_shares and total_shares > 0:
        delta_raw = ( (_safe_float(signed_amount) or 0.0) / float(total_shares) ) * 100.0
        delta_pp_model = floor_pct_3(delta_raw)

        if record.share_percentage_before is not None:
            pp_after_model = floor_pct_3((record.share_percentage_before or 0.0) + (delta_pp_model or 0.0))

    if pp_after_model is not None and record.share_percentage_after is not None:
        a = floor_pct_3(pp_after_model)
        b = floor_pct_3(record.share_percentage_after)
        discrepancy_pp = abs((a or 0.0) - (b or 0.0))
        percent_discrepancy = discrepancy_pp > float(PERCENT_TOL_PP)

    audit = {
        "total_shares_model": total_shares,
        "delta_pp_model": delta_pp_model,
        "pp_after_model": pp_after_model,
        "percent_discrepancy": percent_discrepancy,
        "discrepancy_pp": discrepancy_pp,
    }
    record.audit_flags.update(audit)
    return audit

# Single-record processor
def process_filing_record(
    record: FilingRecord,
    doc_meta: Optional[Union[Dict[str, Any], Any]] = None,
) -> FilingRecord:
    """
    Enrich a FilingRecord with:
      - announcement block (pdf/source links for alerts)
      - document median price
      - market reference snapshot
      - per-transaction suspicious price reasons
      - recomputed % ownership audit
      - gating flags: needs_review, skip_reason
      - reasons list (attached to both audit_flags and record.reasons if present)
    Does NOT modify canonical fields of the record.
    """
    reasons: List[Dict[str, Any]] = []
    row_flags: Dict[str, bool] = {}

    # 1) Announcement block (used by Alerts v2 → adds pdf/source URLs)
    if doc_meta is not None:
        try:
            record.audit_flags["announcement"] = build_announcement_block(doc_meta)
        except Exception as e:
            logger.warning("build_announcement_block failed: %s", e)

    symbol = record.symbol

    # 2) Normalize transactions (supports multiple shapes)
    tx_list = _normalize_price_tx_list(record.price_transaction)

    # 3) Document median price (for within-doc sanity checks)
    tx_prices = []
    for tx in tx_list:
        p = _tx_get_price(tx)
        if p:
            tx_prices.append(p)

    doc_median_price = _compute_document_median_price(tx_prices)
    if doc_median_price is not None:
        record.audit_flags["document_median_price"] = doc_median_price

    # 4) Market reference window
        # 4) Market reference window
    market_ref: Optional[Dict[str, Any]] = None
    try:
        market_ref = get_market_reference(symbol, n_days=MARKET_REF_N_DAYS)
    except Exception as e:
        logger.warning("get_market_reference failed for %s: %s", symbol, e)

    # Attach market reference (if any) and derive row-level reasons.
    if market_ref:
        record.audit_flags["market_reference"] = market_ref

        # Freshness / staleness check
        freshness_days = _safe_float((market_ref or {}).get("freshness_days"))
        if freshness_days is not None and freshness_days > float(PRICE_LOOKBACK_DAYS):
            reasons.append({
                "scope": "row",
                "code": "stale_price",
                "message": f"Market reference price is stale: {freshness_days} days old (>{PRICE_LOOKBACK_DAYS}d).",
                "details": {
                    "freshness_days": freshness_days,
                    "price_lookback_days": PRICE_LOOKBACK_DAYS,
                    "market_ref": market_ref,
                },
            })
            row_flags["stale_price"] = True

        # Missing ref_price even though we have a market_ref block
        ref_price = _safe_float((market_ref or {}).get("ref_price"))
        if ref_price is None:
            reasons.append({
                "scope": "row",
                "code": "missing_price",
                "message": "Market reference price is missing for the relevant date.",
                "details": {"symbol": symbol, "market_ref": market_ref},
            })
            row_flags["missing_price"] = True
    else:
        # Entire market reference block missing – treat as missing_price.
        reasons.append({
            "scope": "row",
            "code": "missing_price",
            "message": "Market reference price is missing for the relevant date.",
            "details": {"symbol": symbol},
        })
        row_flags["missing_price"] = True

    # 4.5) Missing/empty critical fields → alert (even if we still insert)
    missing_fields = []
    for field_name in ("amount_transaction", "holding_before", "holding_after"):
        if getattr(record, field_name, None) is None:
            missing_fields.append(field_name)
    if record.price is None:
        missing_fields.append("price")
    if record.transaction_value is None:
        missing_fields.append("transaction_value")

    if missing_fields:
        reasons.append({
            "scope": "row",
            "code": "missing_required_field",
            "message": f"Missing required field(s): {', '.join(missing_fields)}",
            "details": {
                "missing_fields": missing_fields,
                "symbol": symbol,
                "holder_name": record.holder_name,
            },
        })
        row_flags["missing_required_field"] = True

    # 4.6) Transaction direction sanity (buy/sell vs holdings delta)
    ok_dir, dir_reason = _validate_tx_direction(
        record.holding_before,
        record.holding_after,
        record.transaction_type,
    )
    if not ok_dir and dir_reason:
        reasons.append({
            "scope": "row",
            "code": "mismatch_transaction_type",
            "message": f"Transaction type inconsistent with holdings delta ({dir_reason})",
            "details": {
                "holding_before": record.holding_before,
                "holding_after": record.holding_after,
                "transaction_type": record.transaction_type,
            },
        })
        row_flags["mismatch_transaction_type"] = True
    else:
        # Additional guard: mixed buy/sell with transfer/other in the same doc
        tx_types = {(_tx_get_type(tx) or "").lower() for tx in tx_list}
        has_buy_sell = bool(tx_types & {"buy", "sell"})
        has_other = bool(tx_types - {"buy", "sell", ""})
        transfer_only = (not has_buy_sell) and bool(tx_types) and tx_types <= {"transfer", "other", ""}

        if transfer_only:
            reasons.append({
                "scope": "row",
                "code": "transfer_only_transaction",
                "message": "Transfer/other-only transaction; requires manual handling.",
                "details": {"tx_types": sorted(tx_types)},
            })
            row_flags["transfer_only_transaction"] = True
            record.skip_reason = "transfer_only_transaction"
            record.audit_flags["needs_review"] = True
        elif has_buy_sell and has_other:
            reasons.append({
                "scope": "row",
                "code": "mixed_transaction_type",
                "message": "Buy/Sell transaction appears together with transfer/other in the same document.",
                "details": {
                    "tx_types": sorted(tx_types),
                    "holding_before": record.holding_before,
                    "holding_after": record.holding_after,
                },
            })
            row_flags["mixed_transaction_type"] = True
            record.skip_reason = "mixed_transaction_type"
            record.audit_flags["needs_review"] = True

    # 4.7) price_transaction structure sanity
    pt_invalid = False
    if tx_list:
        for tx in tx_list:
            date_val = None
            tx_type_val = None
            price_val = None
            amt_val = None
            if isinstance(tx, PriceTransaction):
                date_val = tx.transaction_date
                tx_type_val = tx.transaction_type
                price_val = tx.transaction_price
                amt_val = tx.transaction_share_amount
            elif isinstance(tx, dict):
                date_val = tx.get("transaction_date") or tx.get("date")
                tx_type_val = tx.get("transaction_type") or tx.get("type")
                price_val = tx.get("transaction_price") or tx.get("price")
                amt_val = tx.get("transaction_share_amount") or tx.get("amount") or tx.get("amount_transacted")
            if not (date_val and tx_type_val and amt_val):
                pt_invalid = True
                break
    elif record.transaction_type in {"buy", "sell"}:
        # For buy/sell filings we expect at least one transaction detail
        pt_invalid = True

    if pt_invalid:
        reasons.append({
            "scope": "row",
            "code": "invalid_price_transaction",
            "message": "price_transaction entries are missing date/type/amount or empty for buy/sell filings.",
            "details": {
                "transaction_type": record.transaction_type,
                "price_transaction": record.price_transaction,
            },
        })
        row_flags["invalid_price_transaction"] = True


    # 5) Per-transaction suspicious price checks
    any_suspicious = False
    for tx in tx_list:
        suspicious, tx_reasons = _check_tx_price_outlier(
            _tx_get_price(tx), doc_median_price, market_ref
        )
        if tx_reasons:
            reasons.extend(tx_reasons)
        if suspicious:
            any_suspicious = True

    if any_suspicious:
        row_flags["suspicious_price_level"] = True

    # 6) Ownership % recomputation audit
    audit = _recompute_percentages_model(record)
    if audit.get("percent_discrepancy"):
        reasons.append({
            "scope": "row",
            "code": "percent_discrepancy",
            "message": f"Model pp_after deviates from PDF by {audit.get('discrepancy_pp'):.5f} pp",
            "details": audit,
        })
        row_flags["percent_discrepancy"] = True

    # 7) Gate + actionable flags
    needs_review = False
    skip_reason: Optional[str] = None

    for r in reasons:
        code = (r.get("code") or "").strip()
        if code in GATE_REASONS:
            needs_review = True
            skip_reason = code
            break

    if not skip_reason and row_flags.get("suspicious_price_level"):
        needs_review = True
        skip_reason = "suspicious_price_level"

    # 8) Persist flags + reasons for downstream consumers (alerts/email)
    record.audit_flags.update(row_flags)
    record.audit_flags["needs_review"] = needs_review
    record.skip_reason = skip_reason

    # Attach reasons both into audit_flags and (if present) top-level .reasons
    record.audit_flags["reasons"] = reasons
    if hasattr(record, "reasons"):
        try:
            record.reasons = reasons  # enables simple consumers to read reasons directly
        except Exception:
            # If FilingRecord is frozen/immutable in your version, we at least keep audit_flags.reasons
            logger.debug("Could not set record.reasons (immutable?). Reasons kept in audit_flags.")

    return record

# Batch processor
def process_all_records(
    records: List[FilingRecord],
    downloads_meta_map: Dict[str, Any] | None = None,
) -> List[FilingRecord]:
    """
    Process a list of FilingRecords:
      - Resolve document metadata from downloads_meta_map (so alerts can embed urls)
      - Run per-record processing and attach reasons/flags
    """
    processed_records: List[FilingRecord] = []
    for rec in records:
        doc_meta = _resolve_doc_meta(rec, downloads_meta_map)
        processed_records.append(process_filing_record(rec, doc_meta=doc_meta))
    return processed_records
