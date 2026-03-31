# src/generate/filings/utils/pipeline.py
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

# Core transformer
from src.services.alert.schema import build_alert
from src.core.transformer import transform_many

# Pipeline steps
# Import the updated build_ingestion_map
from .loaders import load_parsed_files, build_downloads_meta_map, build_ingestion_map
from .processors import process_all_records
from .consolidators import dedupe_rows
from .matching import matching_investor_and_conglomerates 


log = logging.getLogger("filings.pipeline")

def _stage_log(label: str, count: int, note: str = ""):
    log.info("[STAGE] %-12s → %d records %s", label, count, note)


def run(
    *,
    parsed_files: List[str],
    downloads_file: str,
    output_file: str,
    ingestion_file: str, # This is the argument from cli.py
    alerts_file: Optional[str] = None,
    **kwargs,
) -> int:
    
    # 1) LOAD
    parsed_chunks = load_parsed_files(parsed_files)
    raw_rows: List[Dict[str, Any]] = [row for chunk in parsed_chunks for row in chunk]
    _stage_log("Loaded", len(raw_rows))
    
    # 2) LOAD MAPS
    downloads_meta_map = build_downloads_meta_map(downloads_file)
    # Load ingestion map (now contains full dict)
    ingestion_map = build_ingestion_map(ingestion_file)

    # 3) TRANSFORM (Pass along the new ingestion_map)
    records = transform_many(raw_rows, ingestion_map=ingestion_map)
    _stage_log("Transformed", len(records), "(Standardized to FilingRecord)")

    # 4) PROCESS (Audit, Price Checks)
    records = process_all_records(records, downloads_meta_map)
    _stage_log("Processed", len(records), "(Price checks & audits done)")

    # 5) DEDUPE (In-batch)
    records = dedupe_rows(records)
    _stage_log("Deduped", len(records))

    # 6) Matching for investor and conglomerates slug 
    records = matching_investor_and_conglomerates(records)

    # 7) SAVE filings rows (DB + audit helpers)
    outp = Path(output_file)
    outp.parent.mkdir(parents=True, exist_ok=True)

    output_rows: List[Dict[str, Any]] = []
    for rec in records:
        row = rec.to_db_dict()

        # Aliases expected by downstream alert/email layers
        if "transaction_type" in row and "type" not in row:
            row["type"] = row["transaction_type"]
        if "amount_transaction" in row and "amount" not in row:
            row["amount"] = row["amount_transaction"]
        if "transaction_value" in row and "value" not in row:
            row["value"] = row["transaction_value"]

        # Attach audit flags / reasons so alerts_v2 / email can use them.
        audit = getattr(rec, "audit_flags", None) or {}
        if audit:
            row["audit_flags"] = audit

            # Bubble up a few commonly used flags to top-level
            for k in ("suspicious_price_level", "percent_discrepancy", "stale_price", "missing_price", "delta_pp_mismatch"):
                if k in audit:
                    row[k] = bool(audit.get(k))

            if "needs_review" in audit:
                row["needs_review"] = bool(audit.get("needs_review"))

            if audit.get("reasons"):
                row["reasons"] = audit["reasons"]

            if audit.get("market_reference") is not None:
                row["market_reference"] = audit["market_reference"]

            if audit.get("document_median_price") is not None:
                row["document_median_price"] = audit["document_median_price"]

        # Skip reason is stored on the record itself
        if getattr(rec, "skip_reason", None):
            row["skip_reason"] = rec.skip_reason

        output_rows.append(row)

    outp.write_text(
        json.dumps(output_rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    log.info("[STAGE] Wrote      → %s", outp)

    # 7) Optional alerts (per-row, stage='filings'), using the shared schema.
    if alerts_file:
        alerts_path = Path(alerts_file)
        alerts_path.parent.mkdir(parents=True, exist_ok=True)

        alerts: List[Dict[str, Any]] = []

        for rec, row in zip(records, output_rows):
            audit = getattr(rec, "audit_flags", {}) or {}
            reasons = audit.get("reasons") or []

            # If there are no reasons and the row does not need review, skip.
            if not reasons and not audit.get("needs_review"):
                continue

            ann_block = audit.get("announcement") or {}

            # Basic document context: filename/url/title
            doc_filename = row.get("source") or row.get("filename")
            context_doc_url = ann_block.get("pdf_url") or ann_block.get("url")
            context_doc_title = ann_block.get("title")

            # Group reasons by code so each alert has a primary code
            by_code: Dict[str, List[Dict[str, Any]]] = {}
            for r in reasons:
                code = (r.get("code") or "filings_audit").strip()
                by_code.setdefault(code, []).append(r)

            # If still empty but needs_review is True, synthesize a generic reason
            if not by_code:
                primary_code = (getattr(rec, "skip_reason", None) or "filings_audit").strip() or "filings_audit"
                by_code[primary_code] = [{
                    "scope": "row",
                    "code": primary_code,
                    "message": primary_code,
                    "details": audit,
                }]

            for code, code_reasons in by_code.items():
                ctx: Dict[str, Any] = {
                    "symbol": rec.symbol,
                    "holder_name": rec.holder_name,
                    "type": row.get("type"),
                    "amount": row.get("amount"),
                    "price": row.get("price"),
                    "value": row.get("value"),
                    "share_percentage_before": rec.share_percentage_before,
                    "share_percentage_after": rec.share_percentage_after,
                    "share_percentage_transaction": rec.share_percentage_transaction,
                    "holding_before": rec.holding_before,
                    "holding_after": rec.holding_after,
                    "skip_reason": getattr(rec, "skip_reason", None),
                }

                alert = build_alert(
                    category="inserted",
                    stage="filings",
                    code=code,
                    doc_filename=doc_filename,
                    context_doc_url=context_doc_url,
                    context_doc_title=context_doc_title,
                    announcement=ann_block,
                    reasons=code_reasons,
                    ctx=ctx,
                    needs_review=bool(audit.get("needs_review")),
                )
                # Make symbol/holder/type/amount/price/value easily available at top-level for downstream renderers.
                alert.setdefault("symbol", rec.symbol)
                alert.setdefault("holder_name", rec.holder_name)
                alert.setdefault("type", row.get("type"))
                alert.setdefault("amount", row.get("amount"))
                alert.setdefault("price", row.get("price"))
                alert.setdefault("value", row.get("value"))
                alerts.append(alert)

        alerts_path.write_text(
            json.dumps(alerts, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        log.info("[STAGE] Alerts     → %s (%d alerts)", alerts_path, len(alerts))

    return len(records)
