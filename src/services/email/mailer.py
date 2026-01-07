# src/services/email/mailer.py
from __future__ import annotations

import os
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from .ses_email import send_attachments  # use the function you already wrote


# helpers
def _tolist(x: Optional[Sequence[str] | str]) -> List[str]:
    if x is None:
        return []
    if isinstance(x, str):
        return [s.strip() for s in x.split(",") if s.strip()]
    return [s for s in x if s]


def _esc(s: Any) -> str:
    return ("" if s is None else str(s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _short_url(u: Optional[str]) -> str:
    if not u:
        return "-"
    try:
        parsed = urlparse(u)
        tail = parsed.path.split("/")[-1] or parsed.netloc
        return f"{parsed.netloc}/…/{tail}"
    except Exception:
        return u


def _extract_primary_details(alert: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(alert.get("details"), dict):
        return alert["details"]

    reasons = alert.get("reasons") or []
    if not isinstance(reasons, list):
        return {}

    preferred_scopes = ("system", "tx")
    fallback_details: Optional[Dict[str, Any]] = None

    for r in reasons:
        if not isinstance(r, dict):
            continue
        scope = r.get("scope")
        d = r.get("details")
        if not isinstance(d, dict):
            continue
        if scope in preferred_scopes:
            return d
        if fallback_details is None:
            fallback_details = d

    return fallback_details or {}


def _primary_reason(alert: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    reasons = alert.get("reasons") or []
    if reasons and isinstance(reasons, list):
        r = reasons[0] if reasons else {}
        code = (r.get("code") or "").strip()
        msg = (r.get("message") or "").strip()
        det = r.get("details") if isinstance(r, dict) else None
        return code, msg, det if isinstance(det, dict) else {}
    return "", "", {}


def _suggest_action(alert: Dict[str, Any], stage: str, code: str) -> str:
    cat = alert.get("category") or ""
    if cat == "not_inserted" and stage in ("downloader", "parser"):
        return "Insert manually using the attached link, then re-run."
    if code in ("price_deviation_vs_market", "price_deviation_within_doc", "possible_zero_missing"):
        return "Recalculate price vs reference range, adjust, and re-upload."
    if code in ("percent_discrepancy", "delta_pp_mismatch", "mismatch_transaction_type"):
        return "Review holdings/percentages; correct and re-upload."
    if code in ("symbol_missing", "company_resolve_ambiguous"):
        return "Confirm correct symbol mapping, then re-run."
    if code == "transfer_uid_required":
        return "Pair both sides manually."
    if code in ("missing_price", "stale_price"):
        return "Fetch latest price data and re-run validation."
    return "Review and re-run."


def _flatten_alert_fields(a: Dict[str, Any]) -> Dict[str, Any]:
    details = _extract_primary_details(a)
    ctx = a.get("context") or {}
    if not isinstance(ctx, dict):
        ctx = {}
    doc = ctx.get("doc") or {}
    if not isinstance(doc, dict):
        doc = {}
    ann = a.get("announcement") or ctx.get("announcement") or {}
    if not isinstance(ann, dict):
        ann = {}

    sym = (
        a.get("symbol")
        or a.get("issuer_code")
        or ctx.get("symbol")
        or details.get("symbol")
        or a.get("ticker")
        or a.get("tickers")
        or ""
    )
    if isinstance(sym, list):
        sym = ",".join(str(s) for s in sym)

    holder = (
        a.get("holder_name")
        or ctx.get("holder_name")
        or details.get("holder_name")
        or a.get("holder")
        or a.get("name")
        or "-"
    )

    ttype = (
        a.get("type")
        or details.get("type")
        or a.get("transaction_type")
        or a.get("alert_type")
        or ""
    )

    price = (
        a.get("price")
        or details.get("price")
        or a.get("parsed_price")
    )

    amount = (
        a.get("amount")
        or details.get("amount")
        or a.get("amount_transacted")
        or a.get("shares")
    ) or ""

    value = (
        a.get("value")
        or details.get("value")
        or a.get("transaction_value")
    )

    ts = (
        a.get("timestamp")
        or details.get("timestamp")
        or a.get("transaction_date")
        or details.get("transaction_date")
        or ann.get("published_at")
        or ann.get("publish_date")
        or a.get("date")
        or a.get("time")
        or ""
    )

    src = (
        a.get("source")
        or doc.get("url")
        or doc.get("filename")
        or a.get("doc_url")
        or a.get("url")
        or ann.get("url")
        or ann.get("idx_url")
        or ann.get("link")
        or "-"
    )

    return {
        "symbol": sym,
        "holder": holder,
        "type": ttype,
        "price": price,
        "amount": amount,
        "value": value,
        "timestamp": ts,
        "source": src,
    }


def _render_email_content(alerts: List[Dict[str, Any]],
                          title: str = "IDX Alerts") -> tuple[str, str, str]:
    """
    Return: (subject, body_text, body_html)
    """
    n = len(alerts)
    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"[{title}] {n} alert(s) — {today}"

    # stage / severity counts
    stage_counts: Dict[str, int] = {}
    severity_counts: Dict[str, int] = {}
    for a in alerts:
        stage = (a.get("stage") or "-").lower()
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        sev = (a.get("severity") or "unknown").lower()
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    def _fmt_counts(d: Dict[str, int]) -> str:
        return ", ".join(f"{k}:{v}" for k, v in sorted(d.items()))

    # plain text summary
    lines = [
        f"{title} — {n} alert(s) on {today}",
        f"Stages: {_fmt_counts(stage_counts)}",
        f"Severity: {_fmt_counts(severity_counts)}",
        "-" * 60,
    ]

    rows_html: List[str] = []
    for a in alerts:
        flds = _flatten_alert_fields(a)
        sym = flds["symbol"] or "-"
        holder = flds["holder"] or "-"
        value = flds["value"] or "-"
        ts = flds["timestamp"]
        src = flds["source"]

        stage = a.get("stage") or "-"
        category = a.get('category') or "-"
        code = a.get("code") or "-"
        msg = (a.get("message") or code)
        reason_code, reason_msg, reason_det = _primary_reason(a)

        ctx = a.get("context") or {}
        doc_url = (ctx.get("doc") or {}).get("url") or src
        ann_ctx = ctx.get("announcement") or {}
        ann_url = ann_ctx.get("main_link") or ann_ctx.get("url")

        det = _extract_primary_details(a) or reason_det or {}
        action = _suggest_action(a, stage.lower(), code)

        # text row
        lines.append(
            f"{ts} | {stage} | {category} | {code}: {msg} | action: {action} | "
            f"{sym} holder={holder} val={value} | doc={_short_url(doc_url)} ann={_short_url(ann_url)}"
        )
        if reason_code or reason_msg:
            lines.append(f"  reason: {reason_code or '-'}: {reason_msg or '-'}")

        # html row
        src_link = (
            f'<a href="{_esc(doc_url)}" target="_blank" rel="noopener">{_esc(_short_url(doc_url))}</a>'
            if doc_url else "-"
        )
        ann_link = (
            f'<a href="{_esc(ann_url)}" target="_blank" rel="noopener">{_esc(_short_url(ann_url))}</a>'
            if ann_url else "-"
        )
        reason_display = (reason_code or reason_msg) and f"{_esc(reason_code)}: {_esc(reason_msg)}" or "-"

        rows_html.append(
            "<tr>"
            f"<td>{_esc(ts)}</td>"
            f"<td>{_esc(stage)}</td>"
            f"<td>{_esc(category)}</td>"
            f"<td>{_esc(code)}</td>"
            f"<td>{_esc(msg)}</td>"
            f"<td>{_esc(action)}</td>"
            f"<td><strong>{_esc(sym)}</strong></td>"
            f"<td>{_esc(holder)}</td>"
            f"<td style='text-align:right'>{_esc(value)}</td>"
            f"<td>{src_link}</td>"
            f"<td>{ann_link}</td>"
            f"<td>{reason_display}</td>"
            "</tr>"
        )

    body_text = "\n".join(lines)

    table = (
        "<table style='border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;'>"
        "<thead>"
        "<tr style='background:#f3f4f6'>"
        "<th style='text-align:left;padding:6px;border:1px solid #e5e7eb'>Time</th>"
        "<th style='text-align:left;padding:6px;border:1px solid #e5e7eb'>Stage</th>"
        "<th style='text-align:left;padding:6px;border:1px solid #e5e7eb'>Category</th>"
        "<th style='text-align:left;padding:6px;border:1px solid #e5e7eb'>Code</th>"
        "<th style='text-align:left;padding:6px;border:1px solid #e5e7eb'>Problem</th>"
        "<th style='text-align:left;padding:6px;border:1px solid #e5e7eb'>Action</th>"
        "<th style='text-align:left;padding:6px;border:1px solid #e5e7eb'>Symbol</th>"
        "<th style='text-align:left;padding:6px;border:1px solid #e5e7eb'>Holder</th>"
        "<th style='text-align:right;padding:6px;border:1px solid #e5e7eb'>Value</th>"
        "<th style='text-align:left;padding:6px;border:1px solid #e5e7eb'>Doc</th>"
        "<th style='text-align:left;padding:6px;border:1px solid #e5e7eb'>Announcement</th>"
        "<th style='text-align:left;padding:6px;border:1px solid #e5e7eb'>Reason</th>"
        "</tr>"
        "</thead>"
        "<tbody>" + "".join(rows_html) + "</tbody></table>"
    )

    body_html = (
        f"<div>"
        f"<h2 style='font-family:system-ui,Arial;margin:0 0 8px'>{_esc(title)} — {n} alert(s)</h2>"
        f"<p style='margin:0 0 12px;color:#6b7280'>Date: {today} | Stages: {_esc(_fmt_counts(stage_counts))} | Severity: {_esc(_fmt_counts(severity_counts))}</p>"
        f"{table}"
        f"</div>"
    )

    return subject, body_text, body_html


def send_alerts_email(
    alerts: List[Dict[str, Any]],
    *,
    to: Optional[Sequence[str] | str] = None,
    cc: Optional[Sequence[str] | str] = None,
    bcc: Optional[Sequence[str] | str] = None,
    title: str = "IDX Alerts",
    attach_json_path: Optional[str] = None,
    aws_region: Optional[str] = None,
) -> dict:
    # determine recipients
    to_list = _tolist(to) or _tolist(os.getenv("ALERT_TO_EMAIL")) or _tolist(os.getenv("TEST_TO_EMAIL"))
    if not to_list:
        to_list = ["success@simulator.amazonses.com"]  # safe for sandbox

    subject, body_text, body_html = _render_email_content(alerts, title=title)

    # optional attachment
    files: List[str] = []
    path = attach_json_path
    if path is None:
        path = "./_tmp_alerts_preview.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(alerts, f, ensure_ascii=False, indent=2)
        except Exception:
            path = None
    if path:
        files.append(path)

    return send_attachments(
        to=to_list,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        files=files,
        cc=_tolist(cc),
        bcc=_tolist(bcc),
        aws_region=aws_region,
    )
