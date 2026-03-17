"""
Email delivery service.

Priority order:
  1. SendGrid (SENDGRID_API_KEY)
  2. Postmark  (POSTMARK_SERVER_TOKEN)
  3. SMTP      (SMTP_HOST + SMTP_USER + SMTP_PASSWORD)

Each backend raises on hard failure; the caller logs and marks the record
as 'bounced' rather than crashing the whole campaign.
"""
import os
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

FROM_EMAIL = os.getenv("FROM_EMAIL", "outreach@example.com")
FROM_NAME = os.getenv("FROM_NAME", "Sales Team")

UNSUBSCRIBE_BASE_URL = os.getenv("UNSUBSCRIBE_BASE_URL", "https://your-app.railway.app")


def _unsubscribe_footer(recipient_email: str) -> str:
    return (
        f"\n\n---\n"
        f"To unsubscribe from future emails, click here: "
        f"{UNSUBSCRIBE_BASE_URL}/unsubscribe?email={recipient_email}\n"
        f"This message was sent in compliance with CAN-SPAM and GDPR."
    )


async def send_email(
    to_email: str,
    subject: str,
    body: str,
    campaign_id: Optional[str] = None,
) -> bool:
    """
    Send a single email.  Tries SendGrid → Postmark → SMTP in order.
    Returns True on success, False on failure.
    """
    full_body = body + _unsubscribe_footer(to_email)

    # 1. SendGrid
    sendgrid_key = os.getenv("SENDGRID_API_KEY", "")
    if sendgrid_key:
        try:
            return await _send_via_sendgrid(sendgrid_key, to_email, subject, full_body, campaign_id)
        except Exception as exc:
            logger.warning("SendGrid failed, trying next backend: %s", exc)

    # 2. Postmark
    postmark_token = os.getenv("POSTMARK_SERVER_TOKEN", "")
    if postmark_token:
        try:
            return await _send_via_postmark(postmark_token, to_email, subject, full_body)
        except Exception as exc:
            logger.warning("Postmark failed, trying next backend: %s", exc)

    # 3. SMTP
    smtp_host = os.getenv("SMTP_HOST", "")
    if smtp_host:
        try:
            return _send_via_smtp(smtp_host, to_email, subject, full_body)
        except Exception as exc:
            logger.error("SMTP failed: %s", exc)
            return False

    logger.error("No email backend configured – set SENDGRID_API_KEY, POSTMARK_SERVER_TOKEN, or SMTP_HOST")
    return False


async def _send_via_sendgrid(
    api_key: str,
    to_email: str,
    subject: str,
    body: str,
    campaign_id: Optional[str],
) -> bool:
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": FROM_EMAIL, "name": FROM_NAME},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    if campaign_id:
        payload["custom_args"] = {"campaign_id": campaign_id}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if resp.status_code in (200, 202):
        logger.info("SendGrid sent to %s", to_email)
        return True
    raise RuntimeError(f"SendGrid HTTP {resp.status_code}: {resp.text[:200]}")


async def _send_via_postmark(
    token: str,
    to_email: str,
    subject: str,
    body: str,
) -> bool:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.postmarkapp.com/email",
            headers={
                "X-Postmark-Server-Token": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "From": f"{FROM_NAME} <{FROM_EMAIL}>",
                "To": to_email,
                "Subject": subject,
                "TextBody": body,
            },
        )
    if resp.status_code == 200:
        logger.info("Postmark sent to %s", to_email)
        return True
    raise RuntimeError(f"Postmark HTTP {resp.status_code}: {resp.text[:200]}")


def _send_via_smtp(smtp_host: str, to_email: str, subject: str, body: str) -> bool:
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        server.sendmail(FROM_EMAIL, to_email, msg.as_string())

    logger.info("SMTP sent to %s", to_email)
    return True
