# src/generate/articles/utils/uploader.py
from __future__ import annotations
import os
import json
from typing import Any, Dict, List, Optional, Tuple, Union
from datetime import datetime
import pathlib
import re

# If your class lives in src/services/upload/uploader.py, import from there:
from services.upload.supabase import SupabaseUploader  

from .io_utils import get_logger

log = get_logger(__name__)

# helpers
def _ensure_list(v: Any) -> Optional[List[Any]]:
    """Coerce value to list (or None). Accept list, JSON-encoded list string, or scalar."""
    if v is None:
        return None
    if isinstance(v, list):
        return v if v else None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed if parsed else None
        except Exception:
            return [s]
    return [v]

def _ensure_str_list(v: Any) -> Optional[List[str]]:
    lst = _ensure_list(v)
    if lst is None:
        return None
    out: List[str] = []
    for x in lst:
        if x is None:
            continue
        s = str(x).strip()
        if s:
            out.append(s)
    return out or None

_ISO_TZ_RE = re.compile(r".*T.*([+-]\d{2}:\d{2}|Z)$")

def _coerce_iso_with_z(ts: Optional[str]) -> Optional[str]:
    """
    Normalize to ISO8601. If no offset present, append 'Z' (UTC).
    Accepts common formats: ISO, 'YYYY-MM-DD HH:MM:SS', 'YYYY/MM/DD HH:MM:SS', 'YYYY-MM-DD'.
    """
    if not ts:
        return None
    s = str(ts).strip()
    if not s:
        return None

    # Already ISO?
    try:
        if "T" in s:
            datetime.fromisoformat(s.replace("Z", "+00:00"))
            if not _ISO_TZ_RE.match(s):
                return s + "Z"
            return s
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.isoformat() + "Z"
        except Exception:
            continue

    return s

# columns allowed in idx_news
_ALLOWED_COLS = {
    "title", "body", "source", "timestamp",
    "sector", "sub_sector", "tags", "tickers",
    "dimension", "votes", "score",
}

def _normalize_article_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize to idx_news schema:
    - tickers/tags/sub_sector -> list[str] or NULL
    - timestamp -> ISO (append 'Z' if without offset)
    - dimension/votes/score -> NULL
    - keep only columns in _ALLOWED_COLS
    """
    r = dict(row)

    r["title"] = (r.get("title") or "").strip()
    r["body"] = (r.get("body") or "").strip()
    r["source"] = (r.get("source") or "idx").strip() or "idx"

    r["timestamp"] = _coerce_iso_with_z(r.get("timestamp") or r.get("date"))

    r["tickers"] = _ensure_str_list(r.get("tickers")) or None
    r["tags"] = _ensure_str_list(r.get("tags")) or None
    r["sub_sector"] = _ensure_str_list(r.get("sub_sector")) or None

    r["sector"] = (r.get("sector") or None)

    r["dimension"] = None
    r["votes"] = None
    r["score"] = None

    keep: Dict[str, Any] = {}
    for k in _ALLOWED_COLS:
        keep[k] = r.get(k, None)
    return keep

def _read_json_or_jsonl(path: Union[str, pathlib.Path]) -> List[Dict[str, Any]]:
    p = pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    if p.suffix.lower() == ".jsonl":
        rows: List[Dict[str, Any]] = []
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return rows
    elif p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if isinstance(data.get("rows"), list):
                return data["rows"]
            return [data]
        raise ValueError("Unsupported JSON structure: expected list, dict (or dict.rows)")
    else:
        raise ValueError("Only .json or .jsonl is supported")

def upload_news_file_cli(
    input_path: str,
    table: str = "idx_news",
    dry_run: bool = False,
    timeout: int = 30,  # kept for compatibility; not used in SupabaseUploader
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None,
) -> Tuple[int, int]:
    """
    Upload article file (.json/.jsonl) into `table` (default: idx_news).
    Returns: (success_count, fail_count)
    """
    rows = _read_json_or_jsonl(input_path)
    normed = [_normalize_article_row(r) for r in rows]

    log.info("Loaded %d rows from %s", len(normed), input_path)
    if dry_run:
        preview = normed[0] if normed else {}
        log.info("[DRY-RUN] sample row after normalization: %s", json.dumps(preview, ensure_ascii=False))
        return (0, 0)

    supabase_url = supabase_url or os.getenv("SUPABASE_URL", "")
    supabase_key = supabase_key or os.getenv("SUPABASE_KEY", "")
    if not supabase_url or not supabase_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY in environment.")

    uploader = SupabaseUploader(
        url=supabase_url,
        key=supabase_key,
    )

    res = uploader.upload_records(
        table=table,
        rows=normed,
        allowed_columns=list(_ALLOWED_COLS),
        normalize_keys=False,
        stop_on_first_error=False,
    )
    ok = res.inserted
    bad = len(res.failed_rows)

    if bad:
        log.warning("Some rows failed to insert: %d failed / %d total", bad, len(normed))
        for i, fr in enumerate(res.failed_rows[:5]):
            log.error("Failed row %d: %s", i, fr)
    else:
        log.info("All rows inserted OK: %d", ok)
    return (ok, bad)

__all__ = ["upload_news_file_cli"]
