# src/pipeline/orchestrator.py
from __future__ import annotations
import argparse, json, logging, os, shutil, subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from services.upload.dedup import upload_filings_with_dedup
from services.upload.supabase import SupabaseUploader, UploadResult
from services.alert.ingestion_context import build_ingestion_index
from src.core.types import FILINGS_ALLOWED_COLUMNS
from src.config.config import ALERTS_OUTPUT_DIR

# Timezone for WIB
try:
    import zoneinfo
    JKT = zoneinfo.ZoneInfo("Asia/Jakarta")
except Exception:
    JKT = None

from ingestion.runner import (
    get_ownership_announcements,
    get_ownership_announcements_range,
    save_json as save_ann_json,
)
from downloader.runner import download_pdfs
from downloader.utils.announcement import Announcement

from parser import parser_idx as parser_idx_mod
from parser import parser_non_idx as parser_non_idx_mod

from generate.filings.runner import run as run_generate 
from services.email.bucketize import bucketize as bucketize_alerts
from services.upload.artifacts import make_artifact_zip

from services.email.ses_email import send_attachments
from src.services.email.mailer import _render_email_content, _tolist

from generate.articles.runner import run_from_filings as run_articles_from_filings
from generate.articles.utils.uploader import upload_news_file_cli


LOG = logging.getLogger("orchestrator")

STATE_FILE = Path("data/state/last_run.json")


# Logging / Time utils
def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


# Logging / Time utils
def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


def _load_last_end() -> Optional[datetime]:
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        dt = datetime.fromisoformat(raw["last_end"])
        return dt if dt.tzinfo else dt.replace(tzinfo=JKT)
    
    except Exception:
        return None


def _save_last_end(end: datetime) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"last_end": end.isoformat()}), encoding="utf-8"
    )


def _now_wib() -> datetime:
    if JKT:
        return datetime.now(JKT)
    return datetime.now()  # fallback


def _fmt(dt: datetime, fmt: str) -> str:
    return dt.strftime(fmt)


# IO helpers
def _safe_mkdirs(*dirs: str) -> None:
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)


def _glob_many(patterns: List[str]) -> List[Path]:
    out: List[Path] = []
    for pat in patterns:
        out.extend(Path(".").glob(pat))
    return out


def pre_clean_outputs() -> None:
    targets = [
        "downloads/idx-format",
        "downloads/non-idx-format",
        "alerts_inserted",
        "alerts_not_inserted",
    ]
    for t in targets:
        p = Path(t)
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    # file patterns
    for f in _glob_many([
        "data/*.json",
        "alerts/*.json",
        "artifacts/*.zip",
    ]):
        try:
            f.unlink()
        except Exception:
            pass
    _safe_mkdirs("downloads/idx-format", "downloads/non-idx-format", "data", "alerts", "artifacts")


def _compute_window_from_minutes(window_minutes: int) -> Tuple[str, str, str, str]:
    """
    Return (date_yyyymmdd, start_hhmm, end_hhmm, out_stub).
    Auto-handle cross-midnight via ingestion utils.
    """
    now = _now_wib()
    start = now - timedelta(minutes=window_minutes)
    date = _fmt(start, "%Y%m%d")
    sh, eh = _fmt(start, "%H:%M"), _fmt(now, "%H:%M")
    stub = f"{_fmt(start, '%Y%m%d_%H%M')}_{_fmt(now, '%Y%m%d_%H%M')}"
    return date, sh, eh, stub


def _relocate_alerts_to_alerts_folder(inserted_path: Optional[Path], not_inserted_path: Optional[Path]) -> Tuple[Optional[Path], Optional[Path]]:
    """
    If write_alert_files drops files into artifacts/, move them to alerts/
    so step_bucketize_alerts (from_dir=alerts) can find them.
    """
    if not inserted_path and not not_inserted_path:
        return inserted_path, not_inserted_path

    alerts_dir = Path("alerts")
    alerts_dir.mkdir(parents=True, exist_ok=True)

    def _move(p: Optional[Path]) -> Optional[Path]:
        if not p or not p.exists():
            return p
        if p.parent == alerts_dir:
            return p  # already in alerts
        target = alerts_dir / p.name
        try:
            target.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
            p.unlink(missing_ok=True)
            LOG.info("[ALERTS] relocated %s -> %s", p, target)
            return target
        except Exception as e:
            LOG.warning("[ALERTS] relocate failed for %s: %s (will keep original path)", p, e)
            return p

    return _move(inserted_path), _move(not_inserted_path)


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str)) # Menambahkan default=str
            f.write("\n")

# Enrich timestamps from downloads metadata 
def _load_json_silent(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# Company assets via single-source script
def _run_company_map_cli(script_path: str, subcmd: str) -> subprocess.CompletedProcess:
    """
    Run company_map single-source script with subcommands:
    get | refresh | reset | status | print
    """
    script = Path(script_path)
    if not script.exists():
        raise FileNotFoundError(f"company_map script not found: {script}")
    cmd = [os.environ.get("PYTHON", os.sys.executable), str(script), subcmd]
    LOG.debug("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def step_company_map_ensure(script_path: str) -> None:
    """
    Ensure local cache exists. Try 'get' first; if it fails, try 'refresh'.
    """
    try:
        cp = _run_company_map_cli(script_path, "get")
        if cp.returncode != 0:
            LOG.warning("[COMPANY_MAP] get failed (%s); trying refresh...", cp.returncode)
            cp2 = _run_company_map_cli(script_path, "refresh")
            if cp2.returncode != 0:
                LOG.warning("[COMPANY_MAP] refresh failed: %s", cp2.stderr.strip())
            else:
                LOG.info("[COMPANY_MAP] refreshed.")
        else:
            LOG.info("[COMPANY_MAP] get ok.")
    except Exception as e:
        LOG.warning("[COMPANY_MAP] ensure failed (continue offline): %s", e)


def step_company_map_refresh(script_path: str) -> None:
    try:
        cp = _run_company_map_cli(script_path, "refresh")
        if cp.returncode != 0:
            LOG.warning("[COMPANY_MAP] refresh failed: %s", cp.stderr.strip())
        else:
            LOG.info("[COMPANY_MAP] refresh ok.")
    except Exception as e:
        LOG.warning("[COMPANY_MAP] refresh exception: %s", e)


def step_company_map_status(script_path: str) -> None:
    try:
        cp = _run_company_map_cli(script_path, "status")
        msg = cp.stdout.strip() or cp.stderr.strip()
        if msg:
            for line in msg.splitlines():
                LOG.info("[COMPANY_MAP] %s", line)
    except Exception as e:
        LOG.debug("[COMPANY_MAP] status error: %s", e)


def _run_latest_prices_script(script_path: str, lookback_days: int, use_fallback: bool) -> None:
    """
    Optional: refresh latest_prices.json via dedicated script.
    """
    sp = Path(script_path)
    if not sp.exists():
        LOG.info("[PRICES] latest-prices-script not found (%s) — skip.", sp)
        return
    cmd = [
        os.environ.get("PYTHON", os.sys.executable),
        str(sp),
        "--lookback-days", str(lookback_days),
    ]
    if not use_fallback:
        cmd.append("--no-fallback")
    LOG.debug("exec: %s", " ".join(cmd))
    cp = subprocess.run(cmd, capture_output=True, text=True)
    if cp.returncode != 0:
        LOG.warning("[PRICES] refresh failed: %s", cp.stderr.strip())
    else:
        LOG.info("[PRICES] refresh ok.")


def step_check_company_map(company_map_script: str, max_age_hours: int = 12) -> None:
    step_company_map_status(company_map_script)

    try:
        cp = _run_company_map_cli(company_map_script, "status")
        raw = cp.stdout.strip() or cp.stderr.strip() or "{}"
        payload = json.loads(raw)
        meta = payload.get("local_meta") or {}
        gen = meta.get("generated_at")

        stale = True 
        if gen:
            try:
                from datetime import datetime, timezone
                age_hours = (datetime.now(timezone.utc)
                             - datetime.fromisoformat(gen.replace("Z", "+00:00"))).total_seconds() / 3600.0
                stale = age_hours >= max_age_hours
                LOG.info("[COMPANY_MAP] age=%.2fh (threshold=%dh) -> %s",
                         age_hours, max_age_hours, "REFRESH" if stale else "OK")
            except Exception as e:
                LOG.debug("[COMPANY_MAP] parse generated_at failed: %s -> will refresh", e)

        if stale or not payload.get("local_rows"):
            step_company_map_refresh(company_map_script)
        else:
            LOG.info("[COMPANY_MAP] fresh enough; skip refresh.")
    except Exception as e:
        LOG.info("[COMPANY_MAP] status check failed (%s) -> trying refresh", e)
        step_company_map_refresh(company_map_script)


def step_refresh_company_assets_single_source(
    *,
    company_map_script: str,
    lookback_days: int = 14,
    use_fallback: bool = True,
    latest_prices_script: Optional[str] = None,
) -> None:
    """
    Refresh assets. **Run prices first**, then refresh company_map.
    """
    if latest_prices_script:
        _run_latest_prices_script(latest_prices_script, lookback_days, use_fallback)
    step_company_map_refresh(company_map_script)

# Ingestion / Download / Parse
def step_fetch_announcements(
    *,
    date_yyyymmdd: Optional[str],
    start_hhmm: Optional[str],
    end_hhmm: Optional[str],
    range_from: Optional[str],
    range_to: Optional[str],
    out_path: Path,
    sort_desc: bool = True,
) -> List[Dict[str, Any]]:
    data: List[Dict[str, Any]]
    if date_yyyymmdd:
        LOG.info("[FETCH] Single-day (WIB) %s %s -> %s", date_yyyymmdd, start_hhmm, end_hhmm)
        data = get_ownership_announcements(
            date_yyyymmdd=date_yyyymmdd,
            start_hhmm=start_hhmm,
            end_hhmm=end_hhmm,
            logger_name="ingestion",
        )
    else:
        assert range_from and range_to
        LOG.info("[FETCH] Range (WIB) %s..%s", range_from, range_to)
        data = get_ownership_announcements_range(
            start_yyyymmdd=range_from,
            end_yyyymmdd=range_to,
            start_dt=None,
            end_dt=None,
            logger_name="ingestion",
        )

    # Sorting optional (default = newest first)
    try:
        from ingestion.utils.sorters import sort_announcements
        data = sort_announcements(data, order="desc" if sort_desc else "asc")
    except Exception:
        pass

    save_ann_json(data, out_path, logger_name="ingestion")
    return data


def step_download_pdfs(
    anns: List[Dict[str, Any]],
    *,
    out_idx_dir: Path,
    out_non_idx_dir: Path,
    meta_out: Path,
    alerts_out: Path,
    retries: int = 3,
    min_similarity: int = 80,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    anns_model = [Announcement(**a) for a in anns]
    download_pdfs(
        announcements=anns_model,
        out_idx=str(out_idx_dir),
        out_non_idx=str(out_non_idx_dir),
        meta_out=str(meta_out),
        alerts_out=str(alerts_out),
        retries=retries,
        min_similarity=min_similarity,
        dry_run=dry_run,
        verbose=verbose,
        clean_out=False,
    )


def step_parse_pdfs(
    *,
    idx_folder: Path,
    non_idx_folder: Path,
    idx_output: Path,
    non_idx_output: Path,
    announcements_json: Path,
    parse_non_idx: bool,
) -> None:
    # IDX
    IDXClass = getattr(parser_idx_mod, "IDXParser", None)
    idx_parser = IDXClass(
        pdf_folder=str(idx_folder),
        output_file=str(idx_output),
        announcement_json=str(announcements_json),
    )
    LOG.info("[PARSER] IDXParser = %s", idx_parser.__class__.__name__)
    idx_parser.parse_folder()

    if not parse_non_idx:
        LOG.info("[PARSER] Non-IDX parsing disabled; skipping.")
        return
    
    # Non-IDX
    NonIDXClass = getattr(parser_non_idx_mod, "NonIDXParser", None)
    nonidx_parser = NonIDXClass(
        pdf_folder=str(non_idx_folder),
        output_file=str(non_idx_output),
        announcement_json=str(announcements_json),
    )
    LOG.info("[PARSER] NonIDXParser = %s", nonidx_parser.__class__.__name__)
    nonidx_parser.parse_folder()


# Generate / Alerts / Upload / Artifacts
def step_generate_filings(
    *,
    idx_parsed: Path,
    non_idx_parsed: Path,
    downloads_meta: Path,
    filings_out: Path,
    alerts_out: Path,
) -> int:
    cnt = run_generate(
        parsed_files=[str(non_idx_parsed), str(idx_parsed)],
        downloads_file=str(downloads_meta),
        output_file=str(filings_out),
        alerts_file=str(alerts_out),
    )
    LOG.info("[GENERATE] filings count = %d", cnt)
    return cnt


def step_bucketize_alerts(
    *,
    inserted_dir: Path = Path("alerts_inserted"),
    not_inserted_dir: Path = Path("alerts_not_inserted"),
) -> None:
    sources = [Path("alerts"), Path(ALERTS_OUTPUT_DIR)]

    seen: set[Path] = set()
    total = {"inserted": 0, "not_inserted": 0}

    for src in sources:
        try:
            resolved = src.resolve()
        except Exception:
            resolved = src

        if resolved in seen:
            continue

        seen.add(resolved)

        if not src.exists():
            continue

        stats = bucketize_alerts(
            from_dir=src,
            inserted_dir=inserted_dir,
            not_inserted_dir=not_inserted_dir,
        )

        total["inserted"] += stats.get("inserted", 0)
        total["not_inserted"] += stats.get("not_inserted", 0)

    LOG.info(
        "[BUCKETIZE] inserted=%d not_inserted=%d",
        total["inserted"],
        total["not_inserted"],
    )


# Email helpers & step
def _gather_json_files(dir_path: Path) -> List[Path]:
    if not dir_path.exists() or not dir_path.is_dir():
        return []
    return sorted([p for p in dir_path.glob("*.json") if p.is_file()])


def _coerce_alerts(obj: Any) -> List[Dict[str, Any]]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for k in ("alerts", "data", "items", "results", "rows"):
            if k in obj and isinstance(obj[k], list):
                return [x for x in obj[k] if isinstance(x, dict)]
        return [obj]
    return []


def _load_alerts_from_files(files: List[Path]) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []
    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            alerts.extend(_coerce_alerts(data))
        except Exception as e:
            LOG.warning("[EMAIL] failed reading %s: %s (skipped)", fp, e)
    return alerts


def _pick_attachments(paths: List[Path], max_bytes: int) -> Tuple[List[Path], int, List[Path]]:
    files: List[Tuple[Path, int]] = []
    for p in paths:
        try:
            files.append((p, p.stat().st_size))
        except Exception:
            LOG.warning("[EMAIL] cannot stat %s (skipped)", p)
    files.sort(key=lambda t: t[1])  # smallest first
    picked: List[Path] = []
    total = 0
    for p, sz in files:
        if total + sz <= max_bytes:
            picked.append(p)
            total += sz
    picked_set = set(picked)
    skipped = [p for p, _ in files if p not in picked_set]
    return picked, total, skipped


def step_email_alerts(
    *,
    inserted_dir: Path = Path("alerts_inserted"),
    not_inserted_dir: Path = Path("alerts_not_inserted"),
    to_inserted: Optional[str] = None,
    to_not_inserted: Optional[str] = None,
    cc_inserted: Optional[str] = None,
    cc_not_inserted: Optional[str] = None,
    bcc_inserted: Optional[str] = None,
    bcc_not_inserted: Optional[str] = None,
    aws_region: Optional[str] = None,
    attach_budget_bytes: int = 7_500_000,
) -> None:
    files = _gather_json_files(inserted_dir) + _gather_json_files(not_inserted_dir)

    # remove duplicate legacy if present
    names = {p.name for p in files}
    if "alerts_not_inserted_downloader.json" in names and "low_title_similarity_alerts.json" in names:
        files = [p for p in files if p.name != "low_title_similarity_alerts.json"]
    files = [p for p in files if p.name != "suspicious_alerts.json"]

    if not files:
        LOG.info("[EMAIL] skipped: both alert folders empty")
        return

    alerts = _load_alerts_from_files(files)
    if not alerts:
        LOG.info("[EMAIL] skipped: no alerts parsed")
        return

    picked, total_bytes, skipped = _pick_attachments(files, attach_budget_bytes)
    if skipped:
        LOG.warning("[EMAIL] some attachments skipped due to size: %s", ", ".join(s.name for s in skipped))

    # single recipient list (merge inserted/not_inserted)
    to_list = [s.strip() for s in (to_inserted or "").split(",") if s.strip()]
    if not to_list:
        to_list = [s.strip() for s in (to_not_inserted or "").split(",") if s.strip()]
    if not to_list:
        LOG.info("[EMAIL] skipped: no recipients configured")
        return

    subject, body_text, body_html = _render_email_content(alerts, title="IDX Alerts")
    if picked:
        body_text += "\n\nAttached files:\n" + "\n".join(f"- {p.name}" for p in picked)

    res = send_attachments(
        to=to_list,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        files=[str(p) for p in picked],
        cc=_tolist(cc_inserted) or _tolist(cc_not_inserted) or None,
        bcc=_tolist(bcc_inserted) or _tolist(bcc_not_inserted) or None,
        aws_region=aws_region,
    )
    LOG.info("[EMAIL] result: %s", res)


def step_zip_artifacts(
    *,
    prefix: str = "filings",
    include_pdfs: bool = False,
    artifact_dir: Path = Path("artifacts"),
) -> Path:
    includes = [
        "data/*.json",
        "alerts/*.json",
        "alerts_inserted/*.json",
        "alerts_not_inserted/*.json",
    ]
    if include_pdfs:
        includes += ["downloads/**/*.pdf", "downloads/**/*.PDF"]
    zip_path, manifest = make_artifact_zip(
        prefix=prefix,
        patterns=includes,
        exclude_patterns=["**/__pycache__/**", "**/.DS_Store", "**/.venv/**"],
        out_dir=str(artifact_dir),
        base_dir=".",
    )
    LOG.info("[ARTIFACT] %s (%d files, %.2f MB)",
             zip_path, manifest.total_files, manifest.total_size / (1024*1024))
    return zip_path


# UPDATED FUNCTION
def step_upload_supabase(
    *,
    input_json: Path,
    table: str,
    supabase_url: Optional[str],
    supabase_key: Optional[str],
    stop_on_missing: bool = False, # This argument is no longer used but kept for compatibility
    strict_exit: bool = False,
    send_email: bool = False,  # reserved
) -> None:
    """
    Upload a cleaned JSON file to Supabase using the refactored
    'upload_filings_with_dedup' service.
    """
    if not supabase_url or not supabase_key:
        LOG.warning("SUPABASE_URL/KEY missing; skip upload.")
        return
    
    if not input_json.exists():
        LOG.error("[UPLOAD] Input file not found: %s. Skipping upload.", input_json)
        return

    uploader = SupabaseUploader(url=supabase_url, key=supabase_key)
    
    # 1. Load normalized data
    raw = _load_json_silent(input_json)
    rows = raw["rows"] if isinstance(raw, dict) and "rows" in raw else (raw if isinstance(raw, list) else [])
    
    if not rows:
        LOG.warning("[UPLOAD] No rows found in %s to upload.", input_json)
        return

    # Filter out rows explicitly marked to skip upload (e.g., mixed transaction types)
    before_filter = len(rows)
    rows = [
        r for r in rows
        if r.get("skip_reason") not in {"mixed_transaction_type", "transfer_only_transaction"}
    ]
    skipped = before_filter - len(rows)
    if skipped:
        LOG.info("[UPLOAD] Skipping %d row(s) due to skip_reason (e.g., mixed_transaction_type/transfer_only_transaction).", skipped)

    # 2. Get the list of valid columns from our core type
    try:
        valid_columns = FILINGS_ALLOWED_COLUMNS
        valid_columns.update(["id", "created_at", "source_is_manual"]) 
    except Exception:
        LOG.error("[UPLOAD] Could not get valid columns from FilingRecord. Upload may fail.")
        valid_columns = None # Continue without filtering

    LOG.info("[UPLOAD] Loaded %d rows from %s. Starting deduplication and upload...", len(rows), input_json)

    # 3. Call the deduplication service
    for row in rows: 
        row['source_is_manual'] = False 
        # row['filings_input_source'] = 'automated'

    res, stats = upload_filings_with_dedup(
        uploader=uploader,
        table=table,
        rows=rows,
        allowed_columns=valid_columns,
        stop_on_first_error=False,
    )
    
    LOG.info("[DEDUP] stats=%s", stats)
    LOG.info("[UPLOAD] inserted=%d failed=%d", res.inserted, len(res.failed_rows))
    if getattr(res, "errors", None):
        LOG.error("[UPLOAD] first_error=%r", res.errors[0])
    
    # 4. Handle exit
    if strict_exit and res.failed_rows:
        raise SystemExit(4)


def step_generate_articles(
    *,
    filings_json: Path,
    articles_out: Path,
    company_map_path: str,
    latest_prices_path: str,
    use_llm: bool,
    provider: Optional[str],
    model_name: Optional[str],
    prefer_symbol: bool,
) -> int:
    data = _load_json_silent(filings_json)
    if not isinstance(data, list):
        if isinstance(data, dict) and "rows" in data and isinstance(data["rows"], list):
            data = data["rows"]
        else:
            LOG.error("[ARTICLES] %s must be a JSON array or {'rows': [...]}, got %s", filings_json, type(data).__name__)
            return 0

    proxy_keys = (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy", "PROXY", "GRPC_PROXY_EXP", "grpc_proxy",
    )
    proxy_snapshot = {key: os.environ[key] for key in proxy_keys if key in os.environ} if use_llm else {}

    if use_llm:
        # LLM gRPC clients can fail when forced through HTTP proxies
        for key in proxy_keys:
            os.environ.pop(key, None)

    try:
        articles = run_articles_from_filings(
            data,
            company_map_path=company_map_path,
            latest_prices_path=latest_prices_path,
            use_llm=use_llm,
            model_name=model_name,
            provider=provider,
            prefer_symbol=prefer_symbol,
        )

    finally:
        if use_llm:
            for key in proxy_keys:
                os.environ.pop(key, None)
            os.environ.update(proxy_snapshot)

    _write_jsonl(articles_out, articles)
    LOG.info("[ARTICLES] wrote %d articles -> %s", len(articles), articles_out)
    return len(articles)


# CLI
def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="IDX filings pipeline orchestrator")

    # Run controls
    p.add_argument("--clean", action="store_true", help="Pre-clean outputs before running")
    p.add_argument("-v", "--verbose", action="store_true")

    # Fetch window modes
    g = p.add_mutually_exclusive_group()
    g.add_argument("--window-minutes", type=int, default=120,
                   help="Look back N minutes from now (WIB). Default: 120")
    g.add_argument("--window-hours", type=int, default=None,
                   help="Alternative to window-minutes; hours back from now (WIB)")

    # Explicit date mode
    p.add_argument("--date", default=None, help="YYYYMMDD (WIB). If set, --start-hhmm and --end-hhmm required.")
    p.add_argument("--start-hhmm", default=None, help="HH:MM WIB (with --date)")
    p.add_argument("--end-hhmm", default=None, help="HH:MM WIB (with --date)")

    # Full-day range mode
    p.add_argument("--from-date", dest="from_date", default=None, help="YYYYMMDD (WIB)")
    p.add_argument("--to-date", default=None, help="YYYYMMDD (WIB)")
    
    # NEW ARGUMENT ADDED
    p.add_argument("--ingestion-file", default="data/ingestion.json", 
                   help="Path to ingestion file with announcement dates and links.")
    # END NEW ARGUMENT

    # Downloader
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--min-similarity", type=int, default=80)
    p.add_argument("--dry-run-download", action="store_true")

    # Parser scope
    p.add_argument("--parser", choices=["idx", "non-idx", "both"], default="idx")

    # Upload (filings)
    p.add_argument("--upload", action="store_true", help="Upload filings to Supabase (requires URL/KEY)")
    p.add_argument("--table", default="idx_filings")
    p.add_argument("--supabase-url", default=os.getenv("SUPABASE_URL"))
    p.add_argument("--supabase-key", default=os.getenv("SUPABASE_KEY"))
    p.add_argument("--stop-on-missing", action="store_true")
    p.add_argument("--strict-exit", action="store_true")

    # Artifacts
    p.add_argument("--zip-artifacts", action="store_true")
    p.add_argument("--artifact-prefix", default="filings")
    p.add_argument("--artifact-dir", default="artifacts")
    p.add_argument("--artifact-with-pdfs", action="store_true")

    # Email alerts
    p.add_argument("--email-alerts", action="store_true",
                   help="Send inserted/not_inserted alert emails if available")
    p.add_argument("--email-to-inserted",
                   default=os.getenv("ALERT_TO_EMAIL_INSERTED") or os.getenv("ALERT_TO_EMAIL"),
                   help="Comma-separated recipients for INSERTED alerts")
    p.add_argument("--email-to-not-inserted",
                   default=os.getenv("ALERT_TO_EMAIL_NOT_INSERTED") or os.getenv("ALERT_TO_EMAIL"),
                   help="Comma-separated recipients for NOT_INSERTED alerts")
    p.add_argument("--email-cc-inserted", default=os.getenv("ALERT_CC_EMAIL_INSERTED"))
    p.add_argument("--email-cc-not-inserted", default=os.getenv("ALERT_CC_EMAIL_NOT_INSERTED"))
    p.add_argument("--email-bcc-inserted", default=os.getenv("ALERT_BCC_EMAIL_INSERTED"))
    p.add_argument("--email-bcc-not-inserted", default=os.getenv("ALERT_BCC_EMAIL_NOT_INSERTED"))
    p.add_argument("--email-region", default=os.getenv("AWS_REGION") or os.getenv("SES_REGION"))
    p.add_argument("--email-attach-budget", type=int,
                   default=int(os.getenv("ATTACH_BUDGET_BYTES", "7500000")),
                   help="Total attachment size budget in bytes (default ~7.5MB)")


    # Articles generate & upload news
    p.add_argument("--generate-articles", action="store_true",
                   help="Generate articles.jsonl from filings_data.json")
    p.add_argument("--articles-out", default="data/articles.jsonl",
                   help="Path output articles JSONL")
    # FIX: Replace p.add.argument with p.add_argument
    p.add_argument("--company-map", default="data/company/company_map.json",
                   help="Path to cached company_map used by articles generator")
    # END FIX
    p.add_argument("--latest-prices", default="data/company/latest_prices.json",
                   help="Path to cached latest_prices used by articles generator")
    p.add_argument("--use-llm", action="store_true",
                   help="Use LLM for article summarization/classification")
    p.add_argument("--llm-provider", default=os.getenv("LLM_PROVIDER") or "",
                   help="groq|openai|gemini (leave blank to autodetect via API key)")
    p.add_argument("--llm-model", default=os.getenv("GROQ_MODEL") or os.getenv("OPENAI_MODEL") or os.getenv("GEMINI_MODEL") or "llama-3.3-70b-versatile",
                   help="LLM model name (match the provider)")
    p.add_argument("--prefer-symbol", action="store_true",
                   help="When both tickers and symbol exist, prefer 'symbol'")

    p.add_argument("--upload-news", action="store_true",
                   help="Upload articles JSON/JSONL to Supabase (idx_news)")
    p.add_argument("--news-table", default="idx_news")
    p.add_argument("--news-input", default=None,
                   help="Path to articles (override). Default = --articles-out")
    p.add_argument("--news-dry-run", action="store_true")
    p.add_argument("--news-timeout", type=int, default=int(os.getenv("SUPABASE_TIMEOUT", "30")))

    # Company assets (single-source script based)
    p.add_argument("--refresh-company-assets", action="store_true",
                   help="Refresh company_map (via single-source script) and optional latest_prices before running")
    p.add_argument("--company-map-script", default=os.getenv("COMPANY_MAP_SCRIPT", "company_map_hybrid.py"),
                   help="Path to company_map_hybrid.py single-source script")
    p.add_argument("--latest-prices-script", default=os.getenv("LATEST_PRICES_SCRIPT", ""),
                   help="(Optional) path to refresh latest_prices; skip if empty/missing")
    p.add_argument("--prices-lookback-days", type=int, default=14,
                   help="Lookback days for latest prices refresh (default 14)")
    p.add_argument("--no-price-fallback", action="store_true",
                   help="Disable per-symbol fallback when missing within lookback window")

    return p


def main():
    ap = build_argparser()
    args = ap.parse_args()
    _setup_logging(args.verbose)

    if args.clean:
        LOG.info("[CLEAN] removing generated outputs")
        pre_clean_outputs()

    # 0) company assets
    if args.refresh_company_assets:
        step_refresh_company_assets_single_source(
            company_map_script=args.company_map_script,
            lookback_days=args.prices_lookback_days,
            use_fallback=(not args.no_price_fallback),
            latest_prices_script=(args.latest_prices_script or None),
        )
    else:
        step_check_company_map(args.company_map_script)

    # 1) Decide fetch window
    ann_out = Path(args.ingestion_file)
    anns: List[Dict[str, Any]]

    if args.date:
        if not (args.start_hhmm and args.end_hhmm):
            ap.error("--start-hhmm and --end-hhmm are required with --date")
        anns = step_fetch_announcements(
            date_yyyymmdd=args.date,
            start_hhmm=args.start_hhmm,
            end_hhmm=args.end_hhmm,
            range_from=None,
            range_to=None,
            out_path=ann_out,
        )
    elif args.from_date or args.to_date:
        if not (args.from_date and args.to_date):
            ap.error("--from-date and --to-date must be provided together")
        anns = step_fetch_announcements(
            date_yyyymmdd=None,
            start_hhmm=None,
            end_hhmm=None,
            range_from=args.from_date,
            range_to=args.to_date,
            out_path=ann_out,
        )
    else:
        now = _now_wib()
        last_end = _load_last_end()

        if last_end is not None and args.window_minutes is None and args.window_hours is None:
            start = last_end - timedelta(minutes=5)
        else:
            minutes = args.window_minutes if args.window_minutes is not None else (args.window_hours or 2) * 60
            start = now - timedelta(minutes=minutes)

        if start.date() != now.date():
            anns = step_fetch_announcements(
                date_yyyymmdd=None,
                start_hhmm=None,
                end_hhmm=None,
                range_from=start.strftime("%Y%m%d"),
                range_to=now.strftime("%Y%m%d"),
                out_path=ann_out,
            )
        else:
            anns = step_fetch_announcements(
                date_yyyymmdd=start.strftime("%Y%m%d"),
                start_hhmm=start.strftime("%H:%M"),
                end_hhmm=now.strftime("%H:%M"),
                range_from=None,
                range_to=None,
                out_path=ann_out,
            )

        _save_last_end(now)

    if not anns:
        LOG.info("[FETCH] No announcements found for the window; skipping downstream steps.")
        LOG.info("[DONE]")
        return

    try:
        INGESTION_IDX = build_ingestion_index(str(ann_out))
        LOG.info("[INGESTION-INDEX] indexed %d filenames (main + attachments)", len(INGESTION_IDX))
        # Optional sanity ping: log 1 example mapping if available
        if INGESTION_IDX:
            any_fn = next(iter(INGESTION_IDX.keys()))
            LOG.debug("[INGESTION-INDEX] example key: %s -> company=%s",
                    any_fn, INGESTION_IDX[any_fn].get("company_name"))
    except Exception as e:
        LOG.warning("[INGESTION-INDEX] failed to build index from %s: %s", ann_out, e)
        INGESTION_IDX = {}


    # 2) Download PDFs
    step_download_pdfs(
        anns,
        out_idx_dir=Path("downloads/idx-format"),
        out_non_idx_dir=Path("downloads/non-idx-format"),
        meta_out=Path("data/downloaded_pdfs.json"),
        alerts_out=Path("alerts/low_title_similarity_alerts.json"),
        retries=args.retries,
        min_similarity=args.min_similarity,
        dry_run=args.dry_run_download,
        verbose=args.verbose,
    )

    # 3) Parse PDFs
    idx_out = Path("data/parsed_idx_output.json")
    non_idx_out = Path("data/parsed_non_idx_output.json")
    
    parser_non_idx = args.parser in ("non-idx", "both")

    step_parse_pdfs(
        idx_folder=Path("downloads/idx-format"),
        non_idx_folder=Path("downloads/non-idx-format"),
        idx_output=idx_out,
        non_idx_output=non_idx_out,
        announcements_json=ann_out,
        parse_non_idx=parser_non_idx,
    )

    # 4) Generate filings
    filings_out = Path("data/filings_data.json")
    # Use v2 naming for inserted alerts from filings stage
    suspicious_out = Path("alerts/alerts_inserted_filings.json")
    step_generate_filings(
        idx_parsed=idx_out,
        non_idx_parsed=non_idx_out,
        downloads_meta=Path("data/downloaded_pdfs.json"),
        filings_out=filings_out,
        alerts_out=suspicious_out,
    )
    

    # 4.5) Generate articles
    if args.generate_articles:
        step_generate_articles(
            filings_json=filings_out, # Use the standardized filings_out
            articles_out=Path(args.articles_out),
            company_map_path=args.company_map,
            latest_prices_path=args.latest_prices,
            use_llm=args.use_llm,
            provider=(args.llm_provider or None),
            model_name=args.llm_model,
            prefer_symbol=args.prefer_symbol,
        )

    # 4.6) Upload articles to news 
    if args.upload_news:
        if not (args.news_table and args.news_table.strip()):
            LOG.error("[UPLOAD-NEWS] news table is empty; aborting upload_news step.")
            return
        news_input = Path(args.news_input) if args.news_input else Path(args.articles_out)
        if not news_input.exists():
            LOG.warning("[UPLOAD-NEWS] %s not found; skip upload", news_input)
        else:
            LOG.info("[UPLOAD-NEWS] uploading %s -> table=%s (dry_run=%s)",
                     news_input, args.news_table, args.news_dry_run)
            upload_news_file_cli(
                input_path=str(news_input),
                table=args.news_table,
                dry_run=args.news_dry_run,
            )

    # 5) Bucketize alerts
    step_bucketize_alerts()

    # 6) Email alerts
    if args.email_alerts:
        step_email_alerts(
            inserted_dir=Path("alerts_inserted"),
            not_inserted_dir=Path("alerts_not_inserted"),
            to_inserted=args.email_to_inserted,
            to_not_inserted=args.email_to_not_inserted,
            cc_inserted=args.email_cc_inserted,
            cc_not_inserted=args.email_cc_not_inserted,
            bcc_inserted=args.email_bcc_inserted,
            bcc_not_inserted=args.email_bcc_not_inserted,
            aws_region=args.email_region,
            attach_budget_bytes=args.email_attach_budget,
        )

    # 7) Artifacts (zip)
    if args.zip_artifacts:
        step_zip_artifacts(
            prefix=args.artifact_prefix,
            include_pdfs=args.artifact_with_pdfs,
            artifact_dir=Path(args.artifact_dir),
        )

    # 8) Optional upload filings
    if args.upload:
        step_upload_supabase(
            input_json=filings_out, 
            table=args.table,
            supabase_url=args.supabase_url,
            supabase_key=args.supabase_key,
            stop_on_missing=args.stop_on_missing,
            strict_exit=args.strict_exit,
        )

    LOG.info("[DONE]")


if __name__ == "__main__":
    main()
