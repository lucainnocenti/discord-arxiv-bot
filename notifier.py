"""Out-of-band notifications to the bot maintainer (currently Telegram)."""

import json
import logging
import socket
import urllib.request

logger = logging.getLogger(__name__)


def send_telegram_message(settings, text: str) -> bool:
    """Sends `text` to the configured Telegram chat. Returns True on success."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.info("Telegram not configured; skipping notification.")
        return False
    # Name the machine: the bot runs on several.
    text = f"arxiv-bot on {socket.gethostname()}: {text}"
    if settings.no_send:
        logger.info(f"[NO_SEND] Would send Telegram message:\n{text}")
        return False

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = json.dumps({"chat_id": settings.telegram_chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
        logger.info("Sent Telegram notification.")
        return True
    except Exception as e:
        # Never log the URL: it contains the bot token.
        logger.warning(f"Failed to send Telegram notification: {type(e).__name__}: {getattr(e, 'code', '')}")
        return False
