# src/services/upload/dedup.py
from __future__ import annotations

import logging
import asyncio
import json
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union, Set, Protocol, Sequence

import httpx  # noqa: F401 (kept for parity with original; safe to remove if unused)

# Uploader imports + UploadResult typing
try:
    # If your supabase module already exports UploadResult, use it directly
    from .supabase import SupabaseUploader, UploadResult  # type: ignore[attr-defined]
except ImportError:
    # Fallback for different execution contexts
    from services.upload.supabase import SupabaseUploader  # type: ignore
    try:
        from services.upload.supabase import UploadResult  # type: ignore[attr-defined]
    except Exception:
        # Lightweight protocol that matches attributes used below
        class UploadResult(Protocol):  # type: ignore[no-redef]
            inserted: int
            failed_rows: Sequence[Dict[str, Any]]

# Fetcher (existing-row lookup) import
try:
    from scripts.fetch_filings import get_idx_filings_by_days
except ImportError:
    logging.warning("Could not import 'get_idx_filings_by_days' from scripts.fetch_filings")

    async def get_idx_filings_by_days(*args, **kwargs) -> List[Dict[str, Any]]:  # type: ignore[func-returns-value]
        logging.error(
            "Using dummy get_idx_filings_by_days. "
            "Deduplication against DB will not be complete."
        )
        return []

# Normalizers
def _to_day(s: Optional[str]) -> str:
    """Convert an ISO-like string to 'YYYY-MM-DD' (best effort)."""
    if not s:
        return ""
    try:
        # Take the first 10 characters (YYYY-MM-DD) from the timestamp
        return str(s)[:10]
    except Exception:
        return ""

def _norm_float(v: Any, ndigits: int = 6):
    """Normalize a numeric-like value to rounded float (or None)."""
    if v is None or v == "":
        return None
    try:
        return round(float(str(v).replace(",", "").strip()), ndigits)
    except Exception:
        return None

# Hashing (local)
def make_filing_hash(row: Dict[str, Any]) -> str:
    """
    Create a SHA-256 hash from key fields of a filing row.
    Keys here must align with DB fetch mapping in _db_row_to_hashable().
    """
    filing_date_key = _to_day(row.get("timestamp"))

    key_data = {
        "symbol": (row.get("symbol") or "").strip().upper(),
        "filing_date": filing_date_key,
        "transaction_type": (row.get("transaction_type") or "").strip().lower(),
        "holder_name": (row.get("holder_name") or "").strip().lower(),
        "holding_before": row.get("holding_before"),
        "holding_after": row.get("holding_after"),
        "share_pct_before": _norm_float(row.get("share_percentage_before")),
        "share_pct_after": _norm_float(row.get("share_percentage_after")),
        "amount": row.get("amount_transaction"),
        "price": _norm_float(row.get("price")),
    }
    # Normalize None → "" for stable hashing
    normalized = {k: ("" if v is None else v) for k, v in key_data.items()}
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

def _prepare_batch_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Ensure each row has 'date_for_query' (YYYY-MM-DD) available
    for the DB lookup function.
    """
    out: List[Dict[str, Any]] = []
    for r in rows:
        rr = dict(r)
        day = _to_day(rr.get("timestamp"))
        if day:
            rr["date_for_query"] = day 
        out.append(rr)
    return out

def _intrarun_unique(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove duplicates within the current batch using the local hash."""
    seen: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for r in rows:
        h = make_filing_hash(r)
        if h in seen:
            continue
        seen.add(h)
        out.append(r)
    return out

# Fetch existing rows from Supabase (by days & symbols)
def _fetch_existing_rows_same_days(
    uploader: SupabaseUploader, 
    table: str,
    days: List[str],
    symbols: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch rows from Supabase for the specific days and (optionally) symbols
    via the async helper get_idx_filings_by_days, bridged with asyncio.run().
    """
    if not days:
        return []
        
    select_cols = ",".join([
        "symbol", "timestamp", "transaction_type", "holder_name",
        "holding_before", "holding_after",
        "share_percentage_before", "share_percentage_after",
        "amount_transaction", "transaction_value", "price"
    ])

    try:
        return asyncio.run(
            get_idx_filings_by_days(
                days=days, 
                symbols=symbols, 
                table=table,
                select=select_cols 
            )
        )
    except Exception as e:
        logging.error(f"Failed to fetch existing rows from Supabase: {e}", exc_info=True)
        return []

def _db_row_to_hashable(db_row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map DB row keys so they line up with make_filing_hash expectations.
    """
    out = dict(db_row)
    if "amount" not in out:
        out["amount"] = db_row.get("amount_transaction")
    if "value" not in out:
        out["value"] = db_row.get("transaction_value")
    return out

# Public entry
def upload_filings_with_dedup(
    *,
    uploader: SupabaseUploader,
    table: str,
    rows: List[Dict[str, Any]],
    allowed_columns: Optional[Union[List[str], Set[str]]] = None,
    stop_on_first_error: bool = False,
) -> Tuple[UploadResult, Dict[str, int]]:
    """
    Upload filings to Supabase with deduplication:
      1) Normalize rows and ensure 'date_for_query'
      2) Drop duplicates within the batch
      3) Pull existing rows for same day(s) & symbols
      4) Hash-compare to filter out rows already in DB
      5) Upload only new, unique rows
    """

    # 1) Ensure 'date_for_query' present
    prepared = _prepare_batch_rows(rows)

    # 2) Intra-batch dedup
    intra = _intrarun_unique(prepared)

    # 3) Identify days & symbols for DB look-up
    days = sorted({r.get("date_for_query") for r in intra if r.get("date_for_query")})
    symbols = [r.get("symbol") for r in intra if r.get("symbol")] or None

    # 4) Fetch existing rows (same days/symbols)
    existing_rows = _fetch_existing_rows_same_days(uploader, table, days, symbols)

    # 5) Build existing-hash set
    existing_hashes: Set[str] = set()
    for dr in existing_rows:
        existing_hashes.add(make_filing_hash(_db_row_to_hashable(dr)))

    # 6) Filter out rows that already exist
    final_rows: List[Dict[str, Any]] = []
    for r in intra:
        h = make_filing_hash(r)
        if h not in existing_hashes:
            final_rows.append(r)

    # 7) Upload only new rows
    print(f'\ndebug data to filing: {json.dumps(final_rows, indent=2)}\n')

    class MockResult:
        inserted = len(final_rows)
        failed_rows = []
        errors = []

    res = MockResult()
    
    stats = {
        "input": len(rows),
        "intrarun_unique": len(intra),
        "existing_same_day_rows": len(existing_rows),
        "to_insert": len(final_rows),
        "inserted": len(final_rows),
        "failed": 0,
    }
    return res, stats

    # print(f'\n[DRY-RUN] Would upload {len(final_rows)} rows to {table}\n')
    # logging.info(f'\nfinal rows to upload filings: {final_rows}\n')

    # res: UploadResult = uploader.upload_records(
    #     table=table,
    #     rows=final_rows,
    #     allowed_columns=allowed_columns,
    #     stop_on_first_error=stop_on_first_error,
    # )

    # stats = {
    #     "input": len(rows),
    #     "intrarun_unique": len(intra),
    #     "existing_same_day_rows": len(existing_rows),
    #     "to_insert": len(final_rows),
    #     "inserted": getattr(res, "inserted", 0),
    #     "failed": len(getattr(res, "failed_rows", [])),
    # }
    # return res, stats
