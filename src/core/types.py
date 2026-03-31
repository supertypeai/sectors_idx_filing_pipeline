# src/core/types.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from decimal import Decimal, ROUND_FLOOR, InvalidOperation

# Columns that are truly allowed for uploader 
FILINGS_ALLOWED_COLUMNS = {
    "symbol", "timestamp", "transaction_type", "holder_name",
    "holding_before", "holding_after", "amount_transaction",
    "share_percentage_before", "share_percentage_after", "share_percentage_transaction",
    "price", "transaction_value",
    "price_transaction",          # JSONB; collapsed in to_db_dict()
    "title", "body", "source",
    "sector", "sub_sector",
    "tags",                      
    "holder_type",
    "source_is_manual",
    "idx_investor_slug",
    "idx_conglomerates_group_slug"
}

# Numeric helpers 
PCT_ABS_TOL_AUDIT = Decimal("0.00001")  

def _to_decimal(x):
    if x in (None, ""):
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return None
    
def floor_pct_3(x):
    """
    Floor to 3 decimals (⌊x·10^3⌋/10^3), then normalize.
    Returns float or None.
    """
    d = _to_decimal(x)
    if d is None:
        return None
    # multiply → floor to integer → divide back
    q = (d * Decimal("1e3")).to_integral_value(rounding=ROUND_FLOOR) / Decimal("1e3")
    return float(q.normalize())

def close_pct(a, b, tol: Decimal = PCT_ABS_TOL_AUDIT) -> bool:
    """Absolute tolerance compare in percentage points (e.g., 0.29 vs 0.290001)."""
    da, db = _to_decimal(a), _to_decimal(b)
    if da is None or db is None:
        return False
    return abs(da - db) <= tol


@dataclass
class PriceTransaction:
    """
    Canonical internal representation of a single transaction event.
    This is kept as objects during processing and collapsed for DB insertion.
    """
    transaction_date: Optional[str] = None     
    transaction_type: Optional[str] = None        
    transaction_price: Optional[float] = None
    transaction_share_amount: Optional[int] = None


@dataclass
class FilingRecord:
    """
    Canonical filing record used across the pipeline.
    All sources must be transformed into this structure before processing/upload.
    DB serialization is handled by to_db_dict().
    """
    # Core
    symbol: str
    timestamp: str                  
    transaction_type: str         
    holder_name: str
    company_name: str

    # Holdings
    holding_before: Optional[int] = None
    holding_after: Optional[int] = None
    amount_transaction: Optional[int] = None

    # Percentages
    share_percentage_before: Optional[float] = None
    share_percentage_after: Optional[float] = None
    share_percentage_transaction: Optional[float] = None

    # Price & Value (row-level)
    price: Optional[float] = None
    transaction_value: Optional[float] = None

    # Standardized JSONB (internal list; collapsed for DB in to_db_dict)
    price_transaction: List[PriceTransaction] = field(default_factory=list)

    # Generated content
    title: Optional[str] = None
    body: Optional[str] = None
    # NOTE: This field does NOT exist in DB; we keep it for emails/summaries only.
    purpose_of_transaction: Optional[str] = None

    # Classification
    tags: List[str] = field(default_factory=list)
    sector: Optional[str] = None
    sub_sector: Optional[str] = None

    # Source / Meta
    source: Optional[str] = None
    holder_type: Optional[str] = None
    filings_input_source: Optional[str] = 'automated'

    # Slug columns 
    idx_investor_slug: Optional[str] = None 
    idx_conglomerates_group_slug: Optional[list[str]] = None 

    # Non-DB fields
    raw_data: Dict[str, Any] = field(default_factory=dict, repr=False)
    audit_flags: Dict[str, Any] = field(default_factory=dict, repr=False)
    skip_reason: Optional[str] = None

    # Helpers (private)
    @staticmethod
    def _ensure_date_yyyy_mm_dd(s: Optional[str]) -> Optional[str]:
        """
        Accept "YYYY-MM-DD" or ISO "YYYY-MM-DDTHH:MM:SS[Z]" → return "YYYY-MM-DD".
        """
        if not s:
            return None
        return str(s)[:10]

    def _normalize_pt_list_to_objects(self) -> List[PriceTransaction]:
        """
        Backward compatibility:
        - If self.price_transaction is already List[PriceTransaction], return it.
        - If it's a List[dict] (legacy), convert each dict to PriceTransaction.
        """
        if not self.price_transaction:
            return []

        # Already objects?
        if isinstance(self.price_transaction[0], PriceTransaction):
            return self.price_transaction

        out: List[PriceTransaction] = []
        for item in self.price_transaction:
            if isinstance(item, PriceTransaction):
                out.append(item)
                continue
            if not isinstance(item, dict):
                continue
            out.append(
                PriceTransaction(
                    transaction_date=item.get("transaction_date") or item.get("date"),
                    transaction_type=item.get("transaction_type") or item.get("type"),
                    transaction_price=item.get("transaction_price") or item.get("price"),
                    transaction_share_amount=(
                        item.get("transaction_share_amount")
                        or item.get("amount")
                        or item.get("amount_transacted")
                    ),
                )
            )
        return out

    def _collapse_price_transactions_for_db(self) -> List[Dict[str, Any]]:
        """
        Convert List[PriceTransaction] → DB format (array with a single object):
        [
          {"
            date": "2025-09-15",
            "type": "buy",
            "price": 198,
            "amount_transacted": 143919497},
          ...
        ]
        - Fallback date uses self.timestamp's date when missing.
        - Keeps original ordering.
        """
        items = self._normalize_pt_list_to_objects()
        fallback_day = self._ensure_date_yyyy_mm_dd(getattr(self, "timestamp", None))
        out: List[Dict[str, Any]] = []
        for tx in (items or []):
            d = self._ensure_date_yyyy_mm_dd(getattr(tx, "transaction_date", None)) or fallback_day
            t = getattr(tx, "transaction_type", None) or self.transaction_type or "other"
            p = getattr(tx, "transaction_price", None)
            a = getattr(tx, "transaction_share_amount", None)
            out.append({
                "date": d,
                "type": t,
                "price": p,
                "amount_transacted": a,
            })
        return out

    # Public: serialize for DB

    def to_db_dict(self) -> Dict[str, Any]:
        """
        Convert this dataclass into a safe dict for Supabase (idx_filings).
        - Collapses price_transaction to the array-of-lists DB shape.
        - Ensures sector/sub_sector are present (defaults to 'unknown').
        - Ensures sub_sector is a string (not a list).
        """
        # Exact DB columns we will insert. Keep in sync with table schema!
        ALLOWED_DB_COLUMNS = {
            "symbol", "timestamp", "transaction_type", "holder_name",
            "holding_before", "holding_after", "amount_transaction",
            "share_percentage_before", "share_percentage_after", "share_percentage_transaction",
            "price", "transaction_value",
            "price_transaction",  # serialized below
            "title", "body",
            "tags", "sector", "sub_sector",
            "source", "holder_type", 
            "idx_investor_slug", "idx_conglomerates_group_slug",

            # use for llm generation news 
            "purpose_of_transaction", "company_name"
        }

        db_dict: Dict[str, Any] = {}

        # 1) price_transaction → collapse
        db_dict["price_transaction"] = self._collapse_price_transactions_for_db()

        # 2) remaining allowed fields (skip None values)
        for key in ALLOWED_DB_COLUMNS:
            if key == "price_transaction":
                continue
            val = getattr(self, key, None)
            if val is not None:
                db_dict[key] = val

        # 3) sub_sector must be a single string
        if isinstance(db_dict.get("sub_sector"), list):
            db_dict["sub_sector"] = db_dict["sub_sector"][0] if db_dict["sub_sector"] else None

        # 4) default sector/sub_sector to satisfy NOT NULL constraints (if any)
        if not db_dict.get("sector"):
            db_dict["sector"] = "unknown"
        if not db_dict.get("sub_sector"):
            db_dict["sub_sector"] = "unknown"

        # 5) normalize percentage fields with Decimal rounding (max 5 decimals)
        for key in ("share_percentage_before", "share_percentage_after", "share_percentage_transaction"):
            if key in db_dict:
                db_dict[key] = floor_pct_3(db_dict.get(key))

        return db_dict
