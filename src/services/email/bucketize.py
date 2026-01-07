# src/services/email/bucketize.py
from __future__ import annotations
import argparse
import json, os
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

log = logging.getLogger("alerts.bucketize")

DEFAULT_FROM_DIR = os.getenv("FILINGS_ALERTS_DIR", "artifacts")

# Policy toggles (adjust if needed)
# Only include rows in *Inserted* payloads that have reasons OR needs_review.
# Set to False so we do not silently drop rows that were inserted but lack reasons.
REQUIRE_REASONS_FOR_INSERTED = False

# If your pipeline already gates on these elsewhere, keep this False here.
# Set to True if you also want to exclude rows that contain any of these reasons
# at the bucketizing stage (usually you gate later in the email CLI).
APPLY_GATE_REASONS = False
GATE_REASONS = {
    "suspicious_price_level",
    "percent_discrepancy",
    "stale_price",
    "missing_price",
    "delta_pp_mismatch",
    # Add more e.g. "missing_type", ... if you want to block them here too
}

# Common container keys that might hold the alert rows in a dict payload
KNOWN_ARRAY_KEYS = ("rows", "alerts", "data", "items", "results")

# Legacy/static filenames (kept for compatibility)
INSERTED_CANDIDATES: List[Tuple[str, str]] = [
    ("alerts_idx.json", "alerts_idx.json"),
    ("alerts_non_idx.json", "alerts_non_idx.json"),
    ("correction_filings.json", "correction_filings.json"),
    # Legacy suspicious/inconsistent disabled in favor of v2 alerts_inserted_*.json
    # ("suspicious_alerts.json", "suspicious_alerts.json"),
    # ("inconsistent_alerts.json", "suspicious_alerts.json"),
]

NOT_INSERTED_CANDIDATES: List[Tuple[str, str]] = [
    ("alerts_not_inserted_idx.json", "alerts_not_inserted_idx.json"),
    ("alerts_not_inserted_non_idx.json", "alerts_not_inserted_non_idx.json"),
    # ("low_title_similarity_alerts.json", "low_title_similarity_alerts.json"),
]

# V2 dynamic filenames (emitted by your step_alerts_v2_from_filings)
V2_INSERTED_GLOB = "alerts_inserted_*.json"
V2_NOT_INSERTED_GLOB = "alerts_not_inserted_*.json"



# JSON helpers
def _json_nonempty(p: Path) -> bool:
    """Return True if file exists and contains a non-empty JSON payload.

    - Detects empty list/dict.
    - For dicts: checks common array-holding keys ('alerts', 'rows', 'data', 'items', 'results').
    - If JSON parsing fails but file has size > 0, treat as non-empty (conservative)."""
    if not p.exists():
        return False
    try:
        if p.stat().st_size == 0:
            return False
    except Exception:
        return False

    try:
        raw = p.read_text(encoding="utf-8")
        if not raw.strip():
            return False
        data = json.loads(raw)
    except Exception:
        # If not valid JSON but non-zero size, be conservative: consider non-empty
        return True

    if isinstance(data, list):
        return len(data) > 0
    if isinstance(data, dict):
        for k in KNOWN_ARRAY_KEYS:
            v = data.get(k)
            if isinstance(v, list):
                return len(v) > 0
        return len(data) > 0
    return True


def _load_json(p: Path) -> Any:
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def _dump_json(data: Any, p: Path, *, dry_run: bool = False) -> None:
    if dry_run:
        log.info("[DRY RUN] would write %s (size after filter may vary)", p)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _locate_rows_container(data: Any) -> tuple[str, Optional[str], Optional[list]]:
    """Find rows within a JSON payload.

    Returns (container_type, key, rows):
      - ('list', None, rows)                if data is a list
      - ('dict-key', key, rows)             if data is a dict w/ list under a known key
      - ('none', None, None)                if nothing suitable is found
    """
    if isinstance(data, list):
        return "list", None, data

    if isinstance(data, dict):
        for k in KNOWN_ARRAY_KEYS:
            v = data.get(k)
            if isinstance(v, list):
                return "dict-key", k, v

    return "none", None, None


def _is_inserted_row_worthy(row: dict) -> bool:
    """Row is worthy of inclusion in *Inserted* if it has reasons or needs_review."""
    if not REQUIRE_REASONS_FOR_INSERTED:
        return True
    has_reasons = bool(row.get("reasons"))
    needs_review = bool(row.get("needs_review"))
    if not (has_reasons or needs_review):
        return False

    if APPLY_GATE_REASONS and has_reasons:
        reasons = set(row.get("reasons") or [])
        if reasons.intersection(GATE_REASONS):
            return False
    return True


def _load_raw_passthrough(p: Path) -> Optional[Any]:
    """If JSON parsing fails but the file is non-empty, return the raw text string.

    This is used as a conservative fallback so we don't accidentally drop a file
    just because the JSON is slightly malformed.
    """
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return None
    return raw if raw.strip() else None


def _filter_inserted_payload(src: Path) -> Optional[Any]:
    """Load JSON and filter rows for *Inserted* policy.

    Returns:
      - Filtered JSON payload (same shape as input) if rows remain after filter.
      - None if the payload becomes empty (callers should SKIP writing/copying).
      - If payload does not contain recognizable rows, we fall back to copy-as-is
        (treated as non-empty). In that case, we return the original data.
    """
    try:
        data = _load_json(src)
    except Exception as e:
        log.warning("failed to load JSON (will treat as copy-as-is): %s (%s)", src, e)
        return _load_raw_passthrough(src)

    ctype, key, rows = _locate_rows_container(data)

    # No conventional rows container: keep as-is
    if ctype == "none" or rows is None:
        return data if data else None

    # Filter rows
    filtered = [r for r in rows if isinstance(r, dict) and _is_inserted_row_worthy(r)]

    if not filtered:
        return None

    # Rebuild payload preserving its original shape
    if ctype == "list":
        return filtered

    if ctype == "dict-key":
        data[key] = filtered
        return data

    # Should not reach here, but keep safe fallback
    return None


# NOTE: duplicate helper below is kept but commented to avoid accidental use.
# def _load_raw_passthrough(p: Path) -> Optional[Any]:
#     \"\"\"If parsing fails, but file exists and is non-empty, pass the raw text through.\"\"\"
#     try:
#         raw = p.read_text(encoding=\"utf-8\")
#     except Exception:
#         return None
#     return raw if raw.strip() else None


#-
# File copy helpers
def _ensure_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)


def _copy_verbatim_if_nonempty(src: Path, dst: Path, *, dry_run: bool = False) -> bool:
    if not _json_nonempty(src):
        log.info("skip (empty): %s", src)
        return False
    _ensure_dir(dst.parent)
    if dry_run:
        log.info("[DRY RUN] would copy %s -> %s", src, dst)
        return True
    shutil.copy2(src, dst)
    log.info("copied: %s -> %s", src, dst)
    return True


def _copy_inserted_with_filter(src: Path, dst: Path, *, dry_run: bool = False) -> bool:
    """Copy *Inserted* alerts with filtering policy applied."""
    if not src.exists():
        return False

    # Early exit: if file is trivially empty by size/content, skip.
    if not _json_nonempty(src):
        log.info("skip (empty): %s", src)
        return False

    filtered_payload = _filter_inserted_payload(src)

    if filtered_payload is None:
        # Nothing to write after filtering
        log.info("skip (no rows after filter): %s", src)
        return False

    _ensure_dir(dst.parent)

    # If we got a raw string (parse failure pass-through), write it as-is.
    if isinstance(filtered_payload, str):
        if dry_run:
            log.info("[DRY RUN] would write raw %s", dst)
            return True
        with dst.open("w", encoding="utf-8") as f:
            f.write(filtered_payload)
        log.info("copied (raw passthrough): %s -> %s", src, dst)
        return True

    # Otherwise, write filtered JSON
    if dry_run:
        log.info("[DRY RUN] would write filtered JSON to %s", dst)
        return True

    with dst.open("w", encoding="utf-8") as f:
        json.dump(filtered_payload, f, ensure_ascii=False, indent=2)
    log.info("copied (filtered): %s -> %s", src, dst)
    return True


# Main bucketize routine
def bucketize(
    *,
    from_dir: Path,
    inserted_dir: Path,
    not_inserted_dir: Path,
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    Copy non-empty alert files from `from_dir` into two buckets:
      - `inserted_dir`
      - `not_inserted_dir`

    Supports:
      1) Legacy/static filenames (INSERTED_CANDIDATES & NOT_INSERTED_CANDIDATES)
      2) V2 dynamic (glob):
           alerts_inserted_*.json       -> inserted_dir (with row filtering)
           alerts_not_inserted_*.json   -> not_inserted_dir (verbatim)
    """
    from_dir = from_dir.resolve()
    inserted_dir = inserted_dir.resolve()
    not_inserted_dir = not_inserted_dir.resolve()

    stats = {"inserted": 0, "not_inserted": 0}

    # 1) Legacy/static filenames
    for src_name, final_name in INSERTED_CANDIDATES:
        src = from_dir / src_name
        dst = inserted_dir / final_name
        if _copy_inserted_with_filter(src, dst, dry_run=dry_run):
            stats["inserted"] += 1

    for src_name, final_name in NOT_INSERTED_CANDIDATES:
        src = from_dir / src_name
        dst = not_inserted_dir / final_name
        if _copy_verbatim_if_nonempty(src, dst, dry_run=dry_run):
            stats["not_inserted"] += 1

    # 2) V2 dynamic (glob)
    # Keep original filename (so date/time embedded in filename is preserved)
    for p in sorted(from_dir.glob(V2_INSERTED_GLOB)):
        dst = inserted_dir / p.name
        if _copy_inserted_with_filter(p, dst, dry_run=dry_run):
            stats["inserted"] += 1

    for p in sorted(from_dir.glob(V2_NOT_INSERTED_GLOB)):
        dst = not_inserted_dir / p.name
        if _copy_verbatim_if_nonempty(p, dst, dry_run=dry_run):
            stats["not_inserted"] += 1

    return stats


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Bucketize alerts into alerts_inserted/ and alerts_not_inserted/ "
            "(supports legacy & V2 filenames; filters Inserted rows by policy)"
        )
    )
    p.add_argument(
        "--from",
        dest="from_dir",
        default=DEFAULT_FROM_DIR,
        help=f"Source alerts dir (default: {DEFAULT_FROM_DIR})",
    )
    p.add_argument(
        "--inserted-dir",
        default="alerts_inserted",
        help="Destination dir for inserted alerts (default: alerts_inserted)",
    )
    p.add_argument(
        "--not-inserted-dir",
        default="alerts_not_inserted",
        help="Destination dir for not-inserted alerts (default: alerts_not_inserted)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p


def main() -> None:
    ap = _build_argparser()
    args = ap.parse_args()

    logging.basicConfig(
        level=(logging.DEBUG if args.verbose else logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    stats = bucketize(
        from_dir=Path(getattr(args, "from_dir", DEFAULT_FROM_DIR)),
        inserted_dir=Path(args.inserted_dir),
        not_inserted_dir=Path(args.not_inserted_dir),
        dry_run=args.dry_run,
    )
    log.info("[BUCKETIZE] inserted=%d not_inserted=%d", stats["inserted"], stats["not_inserted"])


if __name__ == "__main__":
    main()
