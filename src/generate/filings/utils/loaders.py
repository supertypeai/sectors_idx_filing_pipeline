# src/generate/filings/utils/loaders.py
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("filings.loaders")

# Path helpers
def _basename(p: str | None) -> str | None:
    if not p:
        return None
    try:
        return Path(p).name
    except Exception:
        return p

def _stem(p: str | None) -> str | None:
    b = _basename(p)
    if not b:
        return None
    try:
        return Path(b).stem
    except Exception:
        return b

# JSON helpers
def load_json(path: str | Path) -> Any:
    """
    Load a JSON file; return None if missing/empty/invalid.
    """
    p = Path(path)
    if not p.exists():
        log.info("[LOAD] missing: %s", p)
        return None
    try:
        txt = p.read_text(encoding="utf-8")
        if not txt.strip():
            log.info("[LOAD] empty file: %s", p)
            return None
        return json.loads(txt)
    except Exception as e:
        log.warning("[LOAD] invalid json %s: %s", p, e)
        return None

def _coerce_list_downloads(data: Any) -> List[dict]:
    """
    downloaded_pdfs.json is a plain list like:
    [
      {"ticker": "...", "title": "...", "url": "...", "filename": "...", "timestamp": "..."},
      ...
    ]
    """
    if data is None:
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    # tolerate {"items":[...]} just in case
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [d for d in data["items"] if isinstance(d, dict)]
    return []

# Parsed files loader (unchanged)
def load_parsed_files(paths: List[str | Path]) -> List[List[dict]]:
    """
    For each path, return the list[dict] contained in that JSON.
    (Used for parsed_idx / parsed_non_idx outputs.)
    """
    out: List[List[dict]] = []
    for p in paths:
        data = load_json(p)
        # parsed outputs may be list or {"items": [...]}
        if isinstance(data, list):
            lst = [d for d in data if isinstance(d, dict)]
        elif isinstance(data, dict) and isinstance(data.get("items"), list):
            lst = [d for d in data["items"] if isinstance(d, dict)]
        else:
            lst = []
        log.info("[LOAD] parsed file %-40s → %d rows", str(p), len(lst))
        out.append(lst)
    return out

# Downloads meta map (for processors/announcement)
def build_downloads_meta_map(path: str | Path) -> Dict[str, Any]:
    """
    Build an index from downloaded_pdfs.json.
    Keys include:
      - url (exact), basename(url), stem(url)
      - filename (exact), basename(filename), stem(filename)
    Normalized fields:
      - main_link = url
      - date = timestamp
    """
    data = load_json(path)
    items = _coerce_list_downloads(data)
    m: Dict[str, Any] = {}

    for it in items:
        if not isinstance(it, dict):
            continue

        url = it.get("url")
        filename = it.get("filename")

        # normalize canonical fields
        it["main_link"] = url or it.get("main_link")
        it["date"] = it.get("timestamp") or it.get("date")

        # generate keys
        keys: List[str] = []
        for k in (url, filename, _basename(url), _basename(filename), _stem(url), _stem(filename)):
            if k:
                keys.append(str(k))

        # index all keys to the same item
        for k in keys:
            m[k] = it

    log.info("[LOAD] downloads meta: %d keys", len(m))
    return m

# Ingestion map (for transformer/source-date resolution)
def build_ingestion_map(path: str | Path) -> Dict[str, Dict[str, Any]]:
    """
    Build a robust lookup map directly from downloaded_pdfs.json.

    Keys (with exact/basename/stem aliases):
      - url
      - filename

    Values = full item dict, normalized with:
      - item["main_link"] = url
      - item["date"] = timestamp
      - (keeps "ticker", "title" if present)
    """
    log.info("Building ingestion map from: %s", path)
    data = load_json(path)
    items = _coerce_list_downloads(data)

    ingestion_map: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue

        url = (
            item.get("url")
            or item.get("main_link")
            or item.get("link")
            or item.get("pdf_url")
        )
        filename = item.get("filename")

        # normalize canonical fields
        item["main_link"] = url or item.get("main_link")
        item["date"] = item.get("timestamp") or item.get("date")

        # candidate keys
        candidate_keys: List[str] = []
        for k in (url, filename, _basename(url), _basename(filename), _stem(url), _stem(filename)):
            if k:
                candidate_keys.append(str(k))

        if not candidate_keys:
            continue

        for k in candidate_keys:
            ingestion_map[k] = item

    log.info("Built ingestion map with %d entries.", len(ingestion_map))
    return ingestion_map
