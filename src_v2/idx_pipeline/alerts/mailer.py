from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from botocore.exceptions import BotoCoreError, ClientError

from idx_pipeline.alerts.build_template import render_email_content, get_data_to_alert
from idx_pipeline.config.settings import (
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
    AWS_REGION, SES_FROM_EMAIL, ALERT_TO_EMAIL
)

import boto3
import logging
import os


LOGGER = logging.getLogger(__name__)


def attach_file(file_path: str, msg: MIMEMultipart):
    try:
        with open(file_path, 'rb') as file:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(file.read())

        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(file_path)}")
        msg.attach(part)

    except Exception as error:
        LOGGER.warning(f"Failed to attach {file_path}: {error}")


def send_alert(
    payload_alert: list[dict[str, any]],
    attachments_path: list[str] | None = None,
):
    if not payload_alert:
        LOGGER.info("No IDX filing alerts to send.")
        return

    subject, body_text, body_html = render_email_content(
        payload_alert, title="IDX Non-Insertable Transaction Alerts"
    )

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = SES_FROM_EMAIL
    msg["To"] = ALERT_TO_EMAIL

    msg_alt = MIMEMultipart("alternative")
    msg_alt.attach(MIMEText(body_text, "plain"))
    msg_alt.attach(MIMEText(body_html, "html"))
    msg.attach(msg_alt)

    if attachments_path:
        for file_path in attachments_path:
            attach_file(file_path, msg)

    ses = boto3.client(
        "ses",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )

    try:
        response = ses.send_raw_email(
            Source=SES_FROM_EMAIL,
            Destinations=[ALERT_TO_EMAIL],
            RawMessage={"Data": msg.as_string()},
        )
        LOGGER.info(f"Email sent! Message ID: {response.get('MessageId')}")

    except ClientError as error:
        error_code = error.response["Error"].get("Code", "Unknown")
        error_message = error.response["Error"].get("Message", "No message provided")
        LOGGER.error(f"[send_idx_filings_alert] AWS ClientError [{error_code}]: {error_message}")

    except BotoCoreError as error:
        LOGGER.error(f"[send_idx_filings_alert] BotoCoreError: {error}")

    except Exception as error:
        LOGGER.error(f"[send_idx_filings_alert] Unexpected error: {error}")


if __name__ == '__main__':
    data_to_alert = get_data_to_alert('data_v2/alert/not_inserted.json')
    send_alert(
        payload_alert=data_to_alert,
        attachments_path=['data_v2/alert/not_inserted.json'],
    )
