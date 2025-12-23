# src/core/transformer.py
from __future__ import annotations
import os
from urllib.parse import urlparse
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from google.genai import types
from google import genai 
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from deep_translator import GoogleTranslator

from src.core.types import FilingRecord, PriceTransaction, floor_pct_3, close_pct
from src.common.strings import to_float, to_int, kebab

import os 
import time 

load_dotenv()

# Tag dictionaries / mappings
TAG_WHITELIST = {
    "takeover", "mesop", "inheritance", "award",
    "share-transfer", "internal-strategy",
    "investment", "divestment", "capital-restructuring", 
    "free_float_requirement", 'repurchase-agreement'
    #"bullish", "bearish", 
}


PURPOSE_TAG_MAP = {
    "akuisisi": "takeover", "acquisition": "takeover",
    "strategi internal": "internal-strategy", "internal strategy": "internal-strategy",
    "pengembangan usaha": "internal-strategy", "business expansion": "internal-strategy",
    "mesop": "mesop", "warisan": "inheritance", "inheritance": "inheritance",
    "penghargaan": "award", "award": "award",
    "transfer": "share-transfer",
    "investasi": "investment", "investment": "investment",
    "divestasi": "divestment", "difvestment": "divestment",
}


# Config: allow fast opt-out or short timeouts for translation
_GEMINI_CLIENT: Optional[Any] = None
_GEMINI_PURPOSE_ENABLED = os.getenv("GEMINI_PURPOSE_ENABLED", "0").lower() in {"1", "true", "yes"}  # default off (prefer non-LLM)
_GEMINI_PURPOSE_TIMEOUT = float(os.getenv("GEMINI_PURPOSE_TIMEOUT", "8"))
_GEMINI_ALLOW_PROXY = os.getenv("GEMINI_ALLOW_PROXY", "0").lower() in {"1", "true", "yes"}

_GOOGLETRANS_CLIENT: Optional[Any] = None
_GOOGLETRANS_ENABLED = os.getenv("GOOGLETRANS_ENABLED", "1").lower() in {"1", "true", "yes"}
_GOOGLETRANS_ALLOW_PROXY = os.getenv("GOOGLETRANS_ALLOW_PROXY", "1").lower() in {"1", "true", "yes"}

# Lazily initialized Gemini client (None when unavailable)
_GEMINI_CLIENT: Optional[Any] = None



def translator(text: str) -> str:   
    try: 
        return GoogleTranslator(source='auto', target='en').translate(text) 
    except Exception as error: 
        logging.error(f"GoogleTranslator failed: {error}. Returning original text.") 
        return text
    
# Small helpers
def _is_url(s: Optional[str]) -> bool:
    if not s:
        return False
    try:
        u = urlparse(s)
        return bool(u.scheme in {"http", "https"} and u.netloc)
    except Exception:
        return False

def _basename(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    return os.path.basename(str(s))

def _stem(s: Optional[str]) -> Optional[str]:
    b = _basename(s)
    if not b:
        return None
    return os.path.splitext(b)[0]

def _pick_first(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None

def _resolve_from_ingestion_map(
    source_hint: Optional[str],
    ingestion_map: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Best-effort resolver for (date, url) for IDX & NON-IDX.
    Tries multiple keys (exact, basename, stem) and multiple url/date fields.
    """
    if not ingestion_map:
        return None, None

    # Candidate keys
    keys = []
    if source_hint:
        keys.extend([source_hint, _basename(source_hint), _stem(source_hint)])
    keys = [k for k in keys if k]

    item = None
    for k in keys:
        if k in ingestion_map:
            item = ingestion_map[k]
            break
        # Try loose matching against map keys
        for mk, mv in ingestion_map.items():
            if mk == k or _basename(mk) == k or _stem(mk) == k:
                item = mv
                break
        if item:
            break

    if not item or not isinstance(item, dict):
        return None, None

    # Prefer explicit fields; keep broad compatibility
    date = _pick_first(
        item.get("date"),
        item.get("timestamp"),
        item.get("announcement_published_at"),
    )
    url = _pick_first(
        item.get("main_link"),
        item.get("link"),
        item.get("url"),
        item.get("pdf_url"),
        item.get("original_url"),
        item.get("public_url"),
    )
    return date, url

def _translate_to_english(text: str) -> str:
    """Translate purpose to English using googletrans when possible, with robust fallbacks."""
    if not text:
        return ""

    # 1) Try googletrans (non-LLM)
    gt = translator(text)

    # Kadang-kadang library balikannya sama persis → anggap gagal
    if gt and gt.strip().lower() != text.strip().lower():
        print(f'\nraw purpose en: {gt}')
        return gt

    s = text.strip().lower()

    # 2) Phrase-level mapping (substring, bukan exact)
    phrase_map = {
        "memperluas basis kepemilikan saham": "expanding the shareholder base",
        "memperluas basis kepemilikan": "expanding the shareholder base",
        "investasi lainnya": "other investment",
        "investasi lain": "other investment",
        "transaksi pribadi": "personal transaction",
        "transaksi pribadi/investasi": "personal investment transaction",
        "kepemilikan pribadi/individu/investasi": "personal / individual investment ownership",
        "kepemilikan pribadi/individu": "personal or individual ownership",
        "kepemilikan pribadi": "personal ownership",
        "pembayaran utang": "debt repayment",
        "pembayaran hutang": "debt repayment",
        "pembayaran pinjaman": "loan repayment",
    }
    for key, val in phrase_map.items():
        if key in s:
            return val

    # 3) Single-word / simple phrases (extend existing 'known')
    known = {
        "bagian dari proses akuisisi": "part of the acquisition process",
        "strategi internal": "internal strategy",
        "pengembangan usaha": "business expansion",
        "investasi": "investment",
        "divestasi": "divestment",
        "sesuai surat pemberitahuan dari": "in accordance with the notification letter",
        "surat pemberitahuan": "in accordance with the notification letter",
        "investation": "investment",
    }

    # Coba exact dulu
    if s in known:
        return known[s]
    # Kalau tidak exact, cek sebagai substring
    for key, val in known.items():
        if key in s:
            return val

    # 4) Fallback terakhir: pakai PURPOSE_TAG_MAP → kategori Inggris
    # (mis: "investasi lainnya" → tag "investment" → "investment")
    for key, mapped_tag in PURPOSE_TAG_MAP.items():
        if key in s:
            # "share-transfer" → "share transfer", dll.
            return mapped_tag.replace("-", " ")

    # 5) Kalau semua gagal, pakai original (supaya tetap ada info)
    logging.warning("No translation found for '%s'. Using original.", text)
    return text


def _to_str(x: Any) -> Optional[str]:
    """Coerce any value to a trimmed string; return None for null/empty."""
    if x is None:
        return None
    s = str(x).strip()
    return s if s != "" else None

def _parse_date_obj(x: Any) -> Optional[datetime]:
    """Best-effort parser for YYYYMMDD or ISO-like strings."""
    if x is None or x == "":
        return None
    if isinstance(x, datetime):
        return x
    s = str(x).strip()

    # Try YYYYMMDD
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d")
        except Exception:
            pass

    # Try ISO (accept a space between date/time)
    try:
        return datetime.fromisoformat(s.replace(" ", "T"))
    except Exception:
        pass

    logging.warning(f"Could not parse date: {x}. Returning None.")
    return None

def _to_iso_date_full(x: Any) -> Optional[str]:
    """Return ISO datetime (YYYY-MM-DDTHH:MM:SS[.ffff]) or None."""
    dt = _parse_date_obj(x)
    return dt.isoformat() if dt else None

def _to_iso_date_short(x: Any) -> Optional[str]:
    """Return ISO date only (YYYY-MM-DD) or None."""
    dt = _parse_date_obj(x)
    return dt.strftime("%Y-%m-%d") if dt else None

def _ensure_date_yyyy_mm_dd(s: Optional[str]) -> Optional[str]:
    """Accept 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS[Z]' and trim to date."""
    if not s:
        return None
    return str(s)[:10]

def _normalize_symbol(sym: Any) -> Optional[str]:
    """Normalize to UPPER + '.JK' suffix if missing."""
    s = _to_str(sym)
    if not s:
        return None
    s = s.upper()
    return s if s.endswith(".JK") else f"{s}.JK"

# Core transaction logic
def _normalize_transaction_type(raw_type: Any, holding_before: Any, holding_after: Any) -> str:
    """
    Prefer explicit type; otherwise infer buy/sell from holding delta.
    If still unknown (incl. strings that contain 'transfer'), fall back to 'other'.
    """
    type_str = (_to_str(raw_type) or "").lower()
    if type_str in {"buy", "sell", "share-transfer", "others", "award", "inheritance", "mesop"}:
        return type_str

    hb = to_int(holding_before)
    ha = to_int(holding_after)
    if hb is not None and ha is not None:
        if ha > hb:
            return "buy"
        if ha < hb:
            return "sell"

    # Any uncertain/ambiguous descriptor defaults to 'other'
    # (including strings containing 'transfer' but not explicitly recognized)
    return "others"

def _build_tx_list_from_list(tx_list: List[Dict[str, Any]], raw_date: Any) -> List[PriceTransaction]:
    """New input format: list of dicts → List[PriceTransaction]. Prefer per-row ISO date if present."""
    out: List[PriceTransaction] = []
    for tx in tx_list or []:
        if not isinstance(tx, dict):
            continue
        # Important: prefer date_iso first; if missing, use 'date' (may already be ISO from Non-IDX);
        # if still missing, fall back to raw_date (publication timestamp).
        date_src = tx.get("date_iso") or tx.get("date") or raw_date

        out.append(PriceTransaction(
            transaction_date=_to_iso_date_short(date_src),
            transaction_type=_to_str(tx.get("type")),
            transaction_price=to_float(tx.get("price")),
            transaction_share_amount=to_int(tx.get("amount") or tx.get("amount_transacted")),
        ))
    return out

def _build_tx_list_from_dict(tx_dict: Dict[str, Any], raw_date: Any) -> List[PriceTransaction]:
    """Legacy input format: dict of lists (prices, amount_transacted, type) → List[PriceTransaction]."""
    out: List[PriceTransaction] = []
    try:
        prices = tx_dict.get("prices", [])
        amounts = tx_dict.get("amount_transacted", [])
        types = tx_dict.get("type", [])
        # Normalize 'type' to list if a single string is provided
        if isinstance(types, str):
            types = [types] * max(len(prices), len(amounts), 1)

        max_len = max(len(prices), len(amounts), len(types) if isinstance(types, list) else 0)
        for i in range(max_len):
            out.append(PriceTransaction(
                transaction_date=_to_iso_date_short(raw_date),
                transaction_type=_to_str(types[i]) if isinstance(types, list) and i < len(types) else "other",
                transaction_price=to_float(prices[i]) if i < len(prices) else None,
                transaction_share_amount=to_int(amounts[i]) if i < len(amounts) else None,
            ))
        return out
    except Exception:
        return []

def _calculate_wap_and_totals(tx_list: List[PriceTransaction]) -> Tuple[Optional[float], Optional[int], Optional[float]]:
    """Compute weighted average price and totals from buy/sell transactions only."""
    total_value, total_amount = 0.0, 0
    for tx in tx_list:
        price, amount = tx.transaction_price, tx.transaction_share_amount
        tx_type = (tx.transaction_type or "").lower()
        if tx_type in {"buy", "sell"}:
            if price is not None and amount is not None and price > 0 and amount > 0:
                total_value += price * amount
                total_amount += amount
    if total_amount == 0:
        return None, None, None
    return total_value / total_amount, total_amount, total_value

def _generate_title_and_body(
    holder_name: str,
    company_name: str,
    tx_type: str,
    amount: Optional[int],
    holding_before: Optional[int],
    holding_after: Optional[int],
    purpose_en: str,
) -> tuple[str, str]:
    """Human-friendly title/body with minimal grammar rules."""
    action_title = tx_type.replace("-", " ").title()
    if tx_type == "buy":
        action_verb = "bought"
        title = f"{holder_name} buys shares of {company_name}"
    elif tx_type == "sell":
        action_verb = "sold"
        title = f"{holder_name} sells shares of {company_name}"
    elif tx_type == "share-transfer":
        action_verb = "transferred"
        title = f"{holder_name} transfers shares of {company_name}"
    elif tx_type == "award":
        action_verb = "was awarded"
        title = f"{holder_name} was awarded shares of {company_name}"
    elif tx_type == "inheritance":
        action_verb = "inherited"
        title = f"{holder_name} inherits shares of {company_name}"
    else:
        action_verb = "executed a transaction for"
        title = f"{holder_name} {action_title} transaction of {company_name}"

    amount_str = f"{amount:,} shares" if amount is not None else "shares"
    body = f"{holder_name} {action_verb} {amount_str} of {company_name}."

    if holding_before is not None and holding_after is not None:
        hb_str, ha_str = f"{holding_before:,}", f"{holding_after:,}"
        if holding_after > holding_before:
            body += f" This increases their holdings from {hb_str} to {ha_str} shares."
        elif holding_after < holding_before:
            body += f" This decreases their holdings from {hb_str} to {ha_str} shares."
        else:
            body += f" Their holdings remain at {ha_str} shares."

    if purpose_en:
        body += f" The stated purpose of the transaction was {purpose_en.lower()}."
    return title, body


def _enrich_sector_from_provider(symbol: Optional[str], sec: Any, sub: Any) -> tuple[str, str]:
    from src.generate.filings.utils.provider import get_company_info
    s = _to_str(sec)
    u = _to_str(sub)
    if s and s.lower() != "unknown" and u and u.lower() != "unknown":
        return kebab(s), kebab(u)
    info = get_company_info(symbol)
    s = s or info.sector
    u = u or info.sub_sector
    return kebab(s) if s else "unknown", kebab(u) if u else "unknown"


def _normalize_tags(raw_tags: Any, purpose_en: str, tx_type: str) -> List[str]:
    tags = set()
    tag_list: List[str] = []

    if isinstance(raw_tags, list):
        tag_list = raw_tags
    elif isinstance(raw_tags, str):
        try:
            tag_list = json.loads(raw_tags)
        except Exception:
            tag_list = [t.strip() for t in raw_tags.split(",")]

    tx_low = (tx_type or "").lower()

    for tag in tag_list:
        t_low = (_to_str(tag) or "").lower()
        if not t_low:
            continue

        if t_low == "share-transfer":
            # Guard: only allow 'share-transfer' tag if tx_type is compatible (old)
            if tx_low not in ("share-transfer", "others", "buy", "sell"):
                continue

        if t_low in TAG_WHITELIST:
            tags.add(t_low)

    # purpose_low = (purpose_en or "").lower()
    # for key, mapped in PURPOSE_TAG_MAP.items():
    #     if key in purpose_low:
    #         if mapped == "share-transfer": 
    #             if tx_low not in ("share-transfer", "others"):
    #                 continue

    #         tags.add(mapped)

    if tx_low in TAG_WHITELIST:
        tags.add(tx_low)

    return sorted(tags)


# Public API
def transform_raw_to_record(
    raw_dict: Dict[str, Any],
    ingestion_map: Dict[str, Dict[str, Any]],
    company_map: Optional[Dict[str, Dict[str, Any]]] = None,  # kept for compat; provider lookup is used below
) -> FilingRecord:
    """
    Convert arbitrary parsed dict to canonical FilingRecord.

    Notes
-
    - Keeps price_transaction as List[PriceTransaction] (internal format),
      so downstream processors can safely access tx.transaction_price, etc.
    - Sector/sub_sector are enriched from provider (company_map cache) when missing/unknown.
    """
    # Purpose (translated)
    raw_purpose = _to_str(raw_dict.get("purpose") or raw_dict.get("purpose_of_transaction"))
    purpose_en = _translate_to_english(raw_purpose)

    # Holdings & transaction type
    holding_before = to_int(raw_dict.get("holding_before"))
    holding_after  = to_int(raw_dict.get("holding_after"))
    tx_type = _normalize_transaction_type(
        raw_dict.get("transaction_type") or raw_dict.get("type"),
        holding_before, holding_after
    )

    # Percentages
    pp_before = floor_pct_3(raw_dict.get("share_percentage_before"))
    pp_after  = floor_pct_3(raw_dict.get("share_percentage_after"))
    pp_tx     = floor_pct_3(raw_dict.get("share_percentage_transaction"))

    # Timestamp & source (ingestion_map → parser → first tx)
        # Timestamp & source (robust resolver for IDX & NON-IDX)
    main_date: Optional[str] = None
    main_source_url: Optional[str] = None

    raw_source = _to_str(
        raw_dict.get("source") or
        raw_dict.get("pdf_path") or
        raw_dict.get("file") or
        raw_dict.get("filename")
    )

    # Case A: parser already stored a URL in 'source'
    if _is_url(raw_source):
        d_map, u_map = _resolve_from_ingestion_map(raw_source, ingestion_map)
        main_source_url = raw_source  # trust the URL from parser
        main_date = main_date or d_map
        # keep looking for date below if still None
    else:
        # Case B: source is a local path/filename → resolve via map
        d_map, u_map = _resolve_from_ingestion_map(raw_source, ingestion_map)
        main_date = main_date or d_map
        main_source_url = main_source_url or u_map
        if not main_source_url:
            # fallbacks from parser fields (common in NON-IDX)
            main_source_url = _to_str(
                raw_dict.get("pdf_url") or
                raw_dict.get("original_url") or
                raw_dict.get("url") or
                raw_dict.get("main_link") or
                raw_dict.get("link")
            )

    # Fallback date from raw fields / transactions
    if not main_date:
        main_date = (
            raw_dict.get("timestamp") or
            raw_dict.get("announcement_published_at") or
            raw_dict.get("date")
        )
    if not main_date:
        txs_for_date = raw_dict.get("transactions")
        if isinstance(txs_for_date, list) and txs_for_date:
            main_date = txs_for_date[0].get("date")

    # Absolute last resort for source_url: if original 'source' is URL, keep it
    if not main_source_url and _is_url(_to_str(raw_dict.get("source"))):
        main_source_url = _to_str(raw_dict.get("source"))

    # INTERNAL price_transaction list
    price_tx_list: List[PriceTransaction] = []
    if isinstance(raw_dict.get("transactions"), list):
        price_tx_list = _build_tx_list_from_list(raw_dict["transactions"], main_date)
    elif isinstance(raw_dict.get("price_transaction"), dict):
        price_tx_list = _build_tx_list_from_dict(raw_dict["price_transaction"], main_date)
    elif isinstance(raw_dict.get("price_transaction"), list):
        # ← NEW: support non-IDX parser that emits list of dicts
        price_tx_list = _build_tx_list_from_list(raw_dict["price_transaction"], main_date)


    # Aggregate price/amount/value (use WAP when needed)
    wap, total_amount_tx, total_value_tx = _calculate_wap_and_totals(price_tx_list)

    amount = to_int(raw_dict.get("amount_transaction") or raw_dict.get("amount"))
    if amount is None:
        amount = total_amount_tx

    value = to_float(raw_dict.get("transaction_value"))
    if value is None:
        value = total_value_tx

    price = to_float(raw_dict.get("price"))
    if price is None and wap is not None:
        price = wap

    if value is None and price is not None and amount is not None:
        value = price * amount

    # Human-friendly content
    holder_name = _to_str(raw_dict.get("holder_name")) or "Unknown Shareholder"
    company_name = _to_str(
        raw_dict.get("company_name_raw") or
        raw_dict.get("company_name") or
        raw_dict.get("symbol")
    ) or "Unknown Company"

    title, body = _generate_title_and_body(
        holder_name, company_name, tx_type, amount,
        holding_before, holding_after, purpose_en
    )

    # Tags (with transfer guard)
    print(f'\nraw tags before normalize: {raw_dict.get("tags")}\n')
    tags = _normalize_tags(raw_dict.get("tags"), purpose_en, tx_type)
    print(f'\ntags after normalize: {tags}\n')

    # Symbol + sector/sub_sector (provider enrichment)
    symbol_norm = _normalize_symbol(raw_dict.get("symbol") or raw_dict.get("issuer_code"))

    # Prefer raw sector if present & not "unknown"; otherwise query provider
    sec_raw = _to_str(raw_dict.get("sector"))
    sub_raw = _to_str(raw_dict.get("sub_sector"))

    if (not sec_raw or sec_raw.lower() == "unknown") or (not sub_raw or sub_raw.lower() == "unknown"):
        # Lazy import to avoid circulars; provider reads data/company/company_map*.json
        try:
            from src.generate.filings.utils.provider import get_company_info
            info = get_company_info(symbol_norm)
            sec = sec_raw or info.sector
            sub = sub_raw or info.sub_sector
        except Exception:
            sec, sub = sec_raw, sub_raw
    else:
        sec, sub = sec_raw, sub_raw

    sector     = kebab(sec) if sec else "unknown"
    sub_sector = kebab(sub) if sub else "unknown"

    # Build record
    record = FilingRecord(
        symbol=symbol_norm,
        timestamp=_to_iso_date_full(main_date),
        transaction_type=tx_type,
        holder_name=holder_name,
        company_name=company_name,

        holding_before=holding_before,
        holding_after=holding_after,
        amount_transaction=amount,

        share_percentage_before=pp_before,
        share_percentage_after=pp_after,
        share_percentage_transaction=pp_tx,

        price=price,
        transaction_value=value,

        title=title,
        body=body,
        purpose_of_transaction=purpose_en,
        price_transaction=price_tx_list,

        tags=tags,
        sector=sector,
        sub_sector=sub_sector,

        source=main_source_url,
        holder_type=_to_str(raw_dict.get("holder_type")),
        filings_input_source='automated',

        raw_data=raw_dict,
    )

    # Alert flags for manual review
    # Pairing/UID checks are important for transfers and "other".
    if (record.transaction_type or "").lower() in {"share-transfer", "other", "others"}:
        record.audit_flags["needs_manual_uid"] = True
        record.audit_flags["reason"] = f"{record.transaction_type} detected"

    return record


def transform_many(
    raw_dicts: List[Dict[str, Any]],
    ingestion_map: Dict[str, Dict[str, Any]],
    company_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[FilingRecord]:
    """Vector wrapper for transform_raw_to_record."""
    out: List[FilingRecord] = []
    for raw in (raw_dicts or []):
        if not isinstance(raw, dict):
            continue
        try:
            out.append(transform_raw_to_record(raw, ingestion_map, company_map=company_map))
        except Exception as e:
            logging.error(f"Failed to transform row: {e}. Row: {raw}", exc_info=True)
    return out
