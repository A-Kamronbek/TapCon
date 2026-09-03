"""Telegram bot notifications for new orders and messages.

Deliberately best-effort: if the token is blank or Telegram is unreachable,
we log and move on. A seller's order must never fail because our bot is down.
"""
import logging

import httpx
from django.conf import settings

logger = logging.getLogger("core.telegram")


def is_configured() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)


def send_message(text: str) -> bool:
    """Returns True if Telegram accepted the message."""
    if not is_configured():
        logger.info("Telegram not configured; skipping notification")
        return False

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = httpx.post(
            url,
            data={
                "chat_id": settings.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        # Deliberately every exception, not just httpx.HTTPError. This runs
        # in `transaction.on_commit`, so it fires inside the request *after*
        # the order row is already saved — anything that escapes here turns a
        # successful order into a 500 the customer sees, and they order again.
        # A malformed token raises httpx.InvalidURL, which is not an
        # HTTPError, so the narrower catch let exactly that through.
        logger.exception("Could not send the Telegram notification")
        return False

    if response.status_code != 200:
        logger.error("Telegram rejected the message: HTTP %s", response.status_code)
        return False
    return True
