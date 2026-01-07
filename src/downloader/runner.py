# src/downloader/runner.py
from __future__ import annotations
import os
from pathlib import Path
from typing import List, Any, Optional, Dict

from src.common.log import get_logger
from src.common.datetime import timestamp_jakarta
from src.common.files import (
    ensure_dir,
    ensure_parent,
    ensure_clean_dir,
    safe_filename_from_url,
    atomic_write_json,
    safe_unlink,
    write_json,
    safe_mkdirs
)

from downloader.utils.classifier import classify_format
from downloader.client import init_http, get_pdf_bytes_minimal, seed_and_retry_minimal
from downloader.utils.announcement import Announcement

from services.alert.schema import build_alert
from services.alert.ingestion_context import build_ingestion_index, resolve_doc_context_from_announcement


"""Download PDFs (IDX / non-IDX) and emit lightweight metadata & alerts."""


# helpers
def _attachment_to_url(att: Any) -> Optional[str]:
    """Extract a URL string from various attachment shapes."""
    if isinstance(att, str):
        s = att.strip()
        return s if s.lower().startswith("http") else None
    if isinstance(att, dict):
        for k in ("url", "link", "href", "download_url", "file_url", "FullSavePath", "filename", "path"):
            v = att.get(k)
            if isinstance(v, str) and v.strip().lower().startswith("http"):
                return v
    return None


def _derive_ticker(title: str, company_name: Optional[str]) -> Optional[str]:
    """Prefer 'company_name'; fallback to [TICKER] pattern in title."""
    if company_name and company_name.strip():
        return company_name.strip().upper()[:5]
    import re
    m = re.search(r"\[([A-Z]{3,5})\s*\]", (title or "").upper())
    return m.group(1) if m else None


def _clean_outputs(paths: Dict[str, str], logger) -> None:
    """
    Hard clean outputs safely:
    - Recreate out_idx & out_non_idx as empty dirs (protected).
    - Remove meta_out & alerts_out files if exist; ensure parents exist.
    """
    out_idx_abs = Path(paths["out_idx"]).expanduser().resolve()
    out_non_abs = Path(paths["out_non_idx"]).expanduser().resolve()
    logger.info("Cleaning output folders:\n  out_idx=%s\n  out_non_idx=%s", out_idx_abs, out_non_abs)

    ensure_clean_dir(out_idx_abs)
    ensure_clean_dir(out_non_abs)

    removed_meta = safe_unlink(paths["meta_out"])
    removed_alerts = safe_unlink(paths["alerts_out"])
    if removed_meta:
        logger.info("Removed meta_out file: %s", paths["meta_out"])
    if removed_alerts:
        logger.info("Removed alerts_out file: %s", paths["alerts_out"])

    # Make sure parents exist for later atomic writes
    ensure_parent(paths["meta_out"])
    ensure_parent(paths["alerts_out"])


def _prepare_outputs(paths: Dict[str, str], logger) -> None:
    """Create required folders & parent dirs."""
    p_idx = ensure_dir(paths["out_idx"]).resolve()
    p_non = ensure_dir(paths["out_non_idx"]).resolve()
    ensure_parent(paths["meta_out"])
    ensure_parent(paths["alerts_out"])
    logger.debug("Using output folders:\n  out_idx=%s\n  out_non_idx=%s", p_idx, p_non)


def _download_with_retries(url: str, out_path: Path, retries: int, logger) -> bool:
    """
    Try minimal GET once, then up to (retries-1) seed+retry attempts.
    Returns True on success.
    """
    # Attempt 1: minimal GET
    try:
        blob = get_pdf_bytes_minimal(url, timeout=60)
        out_path.write_bytes(blob)
        logger.info("Downloaded: %s", out_path)
        return True
    except Exception as e1:
        logger.warning("Minimal GET failed (%s): %s", url, e1)

    # Attempts 2..retries: seed referer then retry
    total = max(1, int(retries))
    for attempt in range(2, total + 1):
        try:
            blob = seed_and_retry_minimal(url, timeout=60)
            out_path.write_bytes(blob)
            logger.info("[retry %d/%d] Downloaded -> %s", attempt, total, out_path)
            return True
        except Exception as e2:
            logger.warning("[retry %d/%d] Failed (%s): %s", attempt, total, url, e2)

    return False


# main 
def download_pdfs(
    announcements: List[Announcement],
    out_idx: str,
    out_non_idx: str,
    meta_out: str,
    alerts_out: str,
    retries: int = 3,
    min_similarity: int = 80,
    dry_run: bool = False,
    verbose: bool = False,
    clean_out: bool = False,
) -> None:
    """
    - Classify title as IDX / NON-IDX / UNKNOWN (fuzzy).
    - IDX     : download main_link.
    - NON-IDX : pass into alert no further processing.
    - UNKNOWN : no download; record alert.
    - Write two JSONs: metadata list & alerts.
    """
    logger = get_logger("downloader", 10 if verbose else 20)

    paths = {
        "out_idx": out_idx,
        "out_non_idx": out_non_idx,
        "meta_out": meta_out,
        "alerts_out": alerts_out,
    }

    # Clean outputs early if requested (delete then create empty)
    if clean_out:
        logger.info("Cleaning outputs...")
        _clean_outputs(paths, logger)
        logger.info("Outputs cleaned.")

    # Setup HTTP (proxies via env; SSL warnings silenced)
    init_http(insecure=True, silence_warnings=True, load_env=True)
    _prepare_outputs(paths, logger)

    idx_map = build_ingestion_index("data/ingestion.json")

    # Log proxy presence (masking value)
    if any(os.getenv(k) for k in ("PROXY", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")):
        logger.info("Proxy detected in environment.")

    records: List[dict] = []
    alerts: List[dict] = []
    now = timestamp_jakarta()

    for i, ann in enumerate(announcements, start=1):
        label, best, sim_idx, sim_non = classify_format(ann.title, threshold=min_similarity)
        logger.info("[%d/%d] %s", i, len(announcements), ann.title)
        logger.info("    Classification: %s (best=%d, idx=%d, non=%d)", label, best, sim_idx, sim_non)

        ticker = _derive_ticker(ann.title, getattr(ann, "company_name", None))

        if label == "UNKNOWN":
            ref_url = ann.main_link or (_attachment_to_url(ann.attachments[0]) if ann.attachments else None)
            ref_filename = safe_filename_from_url(ref_url) if ref_url else None

            ann_trim = idx_map.get(ref_filename.lower()) if ref_filename else None
            doc_ctx = {"filename": ref_filename, "url": ref_url, "title": ann.title}
            if ann_trim:
                meta = resolve_doc_context_from_announcement(ann_trim, ref_filename)
                if meta:
                    doc_ctx.update(meta)

            alerts.append(build_alert(
                category="not_inserted",
                stage="downloader",
                code="low_title_similarity",
                doc_filename=ref_filename,
                context_doc_url=doc_ctx.get("url"),
                context_doc_title=doc_ctx.get("title"),
                announcement=ann_trim,
                ctx={
                    "similarity_idx": sim_idx,
                    "similarity_non_idx": sim_non,
                    "threshold": min_similarity,
                    "policy": "skipped_download_due_to_unknown_classification",
                },
                needs_review=True,
            ))
            logger.info("    Skipped download (UNKNOWN -> low_title_similarity). Alert recorded.")
            continue

        if label == 'NON-IDX':
            urls = [url for url in (_attachment_to_url(attachment) for attachment in ann.attachments) if url] 
            
            if not urls and ann.main_link:
                urls = [ann.main_link] 

            if not urls:
                logger.warning("No URLs found for this announcement (label=%s).", label)
                continue

            for url in urls:
                ref_filename = safe_filename_from_url(url) if url else None
                ann_trim = idx_map.get(ref_filename.lower()) if ref_filename else None
                doc_ctx = {"filename": ref_filename, "url": url, "title": ann.title} 
                if ann_trim:
                    meta = resolve_doc_context_from_announcement(ann_trim, ref_filename)
                    if meta:
                        doc_ctx.update(meta)

                alerts.append(build_alert(
                    category="not_inserted",
                    stage="downloader",
                    code="non_idx_document",
                    doc_filename=ref_filename,
                    context_doc_url=doc_ctx.get("url"),
                    context_doc_title=doc_ctx.get("title"),
                    announcement=ann_trim,
                    ctx={
                        "classification": "NON-IDX",
                        "similarity_idx": sim_idx,
                        "similarity_non_idx": sim_non,
                    },
                    needs_review=True,
                ))

            logger.info("Skipped download (NON-IDX -> alert recorded)")
            continue

        # Build URL list + output folder
        urls: List[str] = []
        out_folder = paths["out_non_idx"]
        if label == "IDX" and ann.main_link:
            urls = [ann.main_link]
            out_folder = paths["out_idx"]

        # elif label == "NON-IDX" and ann.attachments:
        #     urls = [u for u in (_attachment_to_url(x) for x in ann.attachments) if u]
        #     out_folder = paths["out_non_idx"]

        # Dedup & guard
        urls = list(dict.fromkeys(urls))
        if not urls:
            logger.warning("    No URLs found for this announcement (label=%s).", label)
            continue

        # Ensure destination folder exists (in case user changed CLI paths mid-run)
        out_folder_p = ensure_dir(out_folder).resolve()
        logger.debug("Using out folder: %s", out_folder_p)

        for url in urls:
            filename = safe_filename_from_url(url)
            out_path = out_folder_p / filename

            if dry_run:
                logger.info("    [DRY] Would download -> %s -> %s", url, out_path)
                records.append({
                    "ticker": ticker,
                    "title": ann.title,
                    "url": url,
                    "filename": filename,
                    "timestamp": ann.date,
                })
                continue

            ok = _download_with_retries(url, out_path, retries=retries, logger=logger)
            if ok:
                records.append({
                    "ticker": ticker,
                    "title": ann.title,
                    "url": url,
                    "filename": filename,
                    "timestamp": ann.date,
                })
            else:
                # v2 alert: download_failed
                ref_filename = out_path.name
                ann_trim = idx_map.get(ref_filename.lower())
                doc_ctx = {"filename": ref_filename, "url": url, "title": ann.title}
                if ann_trim:
                    meta = resolve_doc_context_from_announcement(ann_trim, ref_filename)
                    if meta:
                        doc_ctx.update(meta)

                alerts.append(build_alert(
                    category="not_inserted",
                    stage="downloader",
                    code="download_failed",
                    doc_filename=ref_filename,
                    context_doc_url=doc_ctx.get("url"),
                    context_doc_title=doc_ctx.get("title"),
                    announcement=ann_trim,
                    ctx={"url": url, "retries": retries},
                    needs_review=True,
                ))

    # Write outputs atomically
    atomic_write_json(paths["meta_out"], records)
    inserted = [a for a in alerts if a.get("category") == "inserted"]
    not_inserted = [a for a in alerts if a.get("category") == "not_inserted"]

    # Legacy file: mirror all not_inserted alerts for readability/compat
    # Legacy file disabled to avoid duplicates/misleading name
    # atomic_write_json(paths["alerts_out"], not_inserted)

    # v2 standardized outputs
    safe_mkdirs("alerts")
    atomic_write_json(Path("alerts") / "alerts_inserted_downloader.json", inserted)   
    atomic_write_json(Path("alerts") / "alerts_not_inserted_downloader.json", not_inserted)

    logger.info(
        "Finished. %d announcements processed. %d files recorded. %d alerts (v2: %d not_inserted).",
        len(announcements), len(records), len(not_inserted), len(not_inserted)
    )
