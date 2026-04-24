from datetime import datetime

import json
import logging
import html 


LOGGER = logging.getLogger(__name__)


def get_data_to_alert(path: str) -> list[dict[str, any]]:
    try:
        with open(path, 'r') as file:
            data_to_alert = json.load(file)
        return data_to_alert
    except Exception as error:
        LOGGER.error(f"Error loading data to alert from {path}: {error}")
        return []


def build_email_subject(title, alerts):
    total = len(alerts)
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


def render_email_content(alerts: list[dict[str, any]], title: str = "IDX Filing Alerts") -> tuple[str, str, str]:
    subject, total, today = build_email_subject(title, alerts)
    body_text = build_plain_text_body(alerts, title, total, today)
    body_html = build_html_body(alerts, title, total, today)
    return subject, body_text, body_html


if __name__ == "__main__":
    data_to_alert = get_data_to_alert('data_v2/alert/not_inserted.json')
    data_to_alert = data_to_alert[:5]
    subject, body_text, body_html = render_email_content(data_to_alert)

    print(f"Subject: {subject}\n")
    print(f"Body (text):\n{body_text}\n")
    print(f"Body (HTML):\n{body_html}\n")


# uv run -m idx_pipeline.alerts.build_template