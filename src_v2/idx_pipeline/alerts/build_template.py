from datetime import datetime

import json
import logging
import html 


LOGGER = logging.getLogger(__name__)

MISMATCH_TITLE = "Inserted with an unresolved holdings mismatch"
MISMATCH_NOTE = (
    "These filings were inserted as filed, but holding_before + net shares does not "
    "equal holding_after and the pipeline could not tell which number is wrong. "
    "Please review them against the PDF."
)


def get_data_to_alert(path: str) -> list[dict[str, any]]:
    try:
        with open(path, 'r') as file:
            data_to_alert = json.load(file)
        return data_to_alert
    
    except Exception as error:
        LOGGER.error(f"Error loading data to alert from {path}: {error}")
        return []


def build_email_subject(title, alerts, mismatch_alerts=None):
    total = len(alerts) + len(mismatch_alerts or [])
    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"[{title}] {total} alert(s) — {today}"
    return subject, total, today


def build_plain_text_body(alerts, title, total, today):
    lines = [f"{title} — {total} alert(s) on {today}", "-" * 40]

    for index, alert in enumerate(alerts, 1):
        symbol = alert.get("symbol", "-")
        date = alert.get("timestamp", "-")
        url = alert.get("source", "-")
        reasons = alert.get("reasons", [])
        reasons_text = "; ".join(reasons) if reasons else "-"

        lines.append(f"{index}. {symbol} | date={date} | src={url}")
        lines.append(f"   reasons: {reasons_text}")

    return "\n".join(lines)


def escape_keyword(value):
    return html.escape(str(value)) if value is not None else "-"


def build_html_body(alerts, title, total, today):
    rows = []
    for alert in alerts:
        symbol = alert.get("symbol", "-")
        date = alert.get("timestamp", "-")
        url = alert.get("source", "-")
        reasons = alert.get("reasons", [])

        link = (
            f'<a href="{escape_keyword(url)}" target="_blank" rel="noopener">{escape_keyword(url)}</a>'
            if url and url != "-"
            else "-"
        )

        reasons_html = (
            "<ul style='margin:0;padding-left:16px'>"
            + "".join(f"<li>{escape_keyword(reason)}</li>" for reason in reasons)
            + "</ul>"
            if reasons
            else "-"
        )

        rows.append(
            "<tr>"
            f"<td style='padding:8px;border:1px solid #e5e7eb'>{escape_keyword(date)}</td>"
            f"<td style='padding:8px;border:1px solid #e5e7eb'><strong>{escape_keyword(symbol)}</strong></td>"
            f"<td style='padding:8px;border:1px solid #e5e7eb;max-width:320px;overflow-wrap:anywhere'>{link}</td>"
            f"<td style='padding:8px;border:1px solid #e5e7eb'>{reasons_html}</td>"
            "</tr>"
        )

    table = (
        "<table style='border-collapse:collapse;width:100%;font-family:system-ui,Arial'>"
        "<thead>"
        "<tr style='background:#f3f4f6'>"
        "<th style='padding:8px;border:1px solid #e5e7eb;text-align:left'>Date</th>"
        "<th style='padding:8px;border:1px solid #e5e7eb;text-align:left'>Symbol</th>"
        "<th style='padding:8px;border:1px solid #e5e7eb;text-align:left'>Source</th>"
        "<th style='padding:8px;border:1px solid #e5e7eb;text-align:left'>Reasons</th>"
        "</tr>"
        "</thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )

    html = (
        f"<div>"
        f"<h2 style='font-family:system-ui,Arial;margin:0 0 8px'>{escape_keyword(title)}</h2>"
        f"<p style='margin:0 0 12px;color:#6b7280'>{total} alert(s) — {today}</p>"
        f"{table}"
        f"</div>"
    )

    return html


def mismatch_sum(alert: dict) -> str:
    def fmt(value):
        return f"{int(value):,}" if value is not None else "-"

    net = alert.get("net_shares") or 0
    sign = "+" if net >= 0 else "−"

    return (
        f"{fmt(alert.get('holding_before'))} {sign} {fmt(abs(net))} = "
        f"{fmt(alert.get('expected_holding_after'))} vs filed {fmt(alert.get('filed_holding_after'))}"
    )


def build_mismatch_text(mismatch_alerts) -> str:
    lines = ["", MISMATCH_TITLE, "-" * 40, MISMATCH_NOTE]

    for index, alert in enumerate(mismatch_alerts, 1):
        lines.append(
            f"{index}. {alert.get('pdf_id')} {alert.get('symbol', '-')} | "
            f"{alert.get('holder_name', '-')} | date={alert.get('timestamp', '-')}"
        )
        lines.append(f"   flagged : {mismatch_sum(alert)}")
        lines.append(f"   found by: {'; '.join(alert.get('evidence') or ['-'])}")
        lines.append(f"   src     : {alert.get('source', '-')}")

        if alert.get("securities_report_url"):
            lines.append(f"   report  : {alert.get('securities_report_url')}")

    return "\n".join(lines)


def build_mismatch_html(mismatch_alerts) -> str:
    cell = "padding:8px;border:1px solid #e5e7eb;vertical-align:top"
    rows = []

    for alert in mismatch_alerts:
        url = alert.get("source")
        link = (
            f"<a href='{escape_keyword(url)}' target='_blank' rel='noopener'>{escape_keyword(alert.get('pdf_id'))}</a>"
            if url else escape_keyword(alert.get("pdf_id"))
        )
        evidence = "".join(
            f"<li>{escape_keyword(item)}</li>" for item in alert.get("evidence") or []
        )

        report_url = alert.get("securities_report_url")
        if report_url:
            evidence += (
                f"<li><a href='{escape_keyword(report_url)}' target='_blank' rel='noopener'>"
                "securities report</a></li>"
            )

        rows.append(
            "<tr>"
            f"<td style='{cell}'>{escape_keyword(alert.get('timestamp'))}</td>"
            f"<td style='{cell}'><strong>{escape_keyword(alert.get('symbol'))}</strong><br>{escape_keyword(alert.get('holder_name'))}</td>"
            f"<td style='{cell}'>{link}</td>"
            f"<td style='{cell}'>{escape_keyword(mismatch_sum(alert))}</td>"
            f"<td style='{cell}'><ul style='margin:0;padding-left:16px'>{evidence}</ul></td>"
            "</tr>"
        )

    header = "".join(
        f"<th style='padding:8px;border:1px solid #e5e7eb;text-align:left'>{name}</th>"
        for name in ("Date", "Symbol / holder", "Filing", "Flagged", "Found by")
    )

    return (
        f"<h3 style='font-family:system-ui,Arial;margin:24px 0 4px'>{escape_keyword(MISMATCH_TITLE)}</h3>"
        f"<p style='margin:0 0 12px;color:#6b7280'>{escape_keyword(MISMATCH_NOTE)}</p>"
        "<table style='border-collapse:collapse;width:100%;font-family:system-ui,Arial'>"
        f"<thead><tr style='background:#f3f4f6'>{header}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def render_email_content(
    alerts: list[dict[str, any]],
    title: str = "IDX Filing Alerts",
    mismatch_alerts: list[dict[str, any]] | None = None,
) -> tuple[str, str, str]:
    subject, total, today = build_email_subject(title, alerts, mismatch_alerts)
    body_text = build_plain_text_body(alerts, title, total, today)
    body_html = build_html_body(alerts, title, total, today)

    # An empty not-inserted table reads like a broken email when the alert is only about mismatches
    if not alerts:
        body_text = f"{title} — {total} alert(s) on {today}"
        body_html = (
            f"<div><h2 style='font-family:system-ui,Arial;margin:0 0 8px'>{escape_keyword(title)}</h2>"
            f"<p style='margin:0 0 12px;color:#6b7280'>{total} alert(s) — {today}</p></div>"
        )

    if mismatch_alerts:
        body_text += "\n" + build_mismatch_text(mismatch_alerts)
        body_html = body_html[:-len("</div>")] + build_mismatch_html(mismatch_alerts) + "</div>"

    return subject, body_text, body_html


if __name__ == "__main__":
    data_to_alert = get_data_to_alert('data_v2/alert/not_inserted.json')
    data_to_alert = data_to_alert[:5]
    subject, body_text, body_html = render_email_content(data_to_alert)

    print(f"Subject: {subject}\n")
    print(f"Body (text):\n{body_text}\n")
    print(f"Body (HTML):\n{body_html}\n")


# uv run -m idx_pipeline.alerts.build_template