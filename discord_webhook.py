"""Posts messages to a Discord channel through an incoming webhook.

A webhook is a single HTTPS request per message: no bot login, no gateway
connection. It can only post to its own channel, which is all the paper
announcements need.
"""

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request

# Discord's edge rejects requests without a descriptive User-Agent.
USER_AGENT = "DiscordBot (https://github.com/lucainnocenti/discord-arxiv-bot, 1.0)"
MAX_RATE_LIMIT_WAIT_SECONDS = 30
# Small gap between messages so a burst of announcements stays in order and
# well below the webhook rate limit.
SEND_INTERVAL_SECONDS = 1

logger = logging.getLogger("DiscordWebhook")


class WebhookSender:
    """Async `send(message) -> bool` for Pipeline, backed by a webhook URL."""

    def __init__(self, url: str):
        self.url = url

    async def __call__(self, message: str) -> bool:
        delivered = await asyncio.to_thread(self._post, message)
        if delivered:
            await asyncio.sleep(SEND_INTERVAL_SECONDS)
        return delivered

    def _post(self, message: str) -> bool:
        payload = json.dumps({
            "content": message,
            # Ping mentioned authors (<@id>) but never @everyone/@here or roles.
            "allowed_mentions": {"parse": ["users"]},
        }).encode("utf-8")
        # ?wait=true makes Discord confirm the message was actually created.
        request = urllib.request.Request(
            f"{self.url}?wait=true",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )

        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    response.read()
                return True
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    wait = _retry_after_seconds(e)
                    logger.warning(f"Discord webhook rate limited; retrying in {wait:.1f}s.")
                    time.sleep(wait)
                    continue
                # Never log the URL: it works like a password for the channel.
                logger.error(f"Discord webhook rejected the message: HTTP {e.code} {_error_body(e)}")
                return False
            except Exception as e:
                logger.error(f"Could not reach the Discord webhook: {type(e).__name__}: {e}")
                return False
        return False


def _retry_after_seconds(error: urllib.error.HTTPError) -> float:
    try:
        seconds = float(json.loads(error.read() or b"{}").get("retry_after", 1))
    except Exception:
        seconds = 1.0
    return min(max(seconds, 0.5), MAX_RATE_LIMIT_WAIT_SECONDS)


def _error_body(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", errors="replace")[:200]
    except Exception:
        return ""
