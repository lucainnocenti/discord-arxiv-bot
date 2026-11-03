import asyncio
import io
import json
import unittest
import urllib.error
from unittest.mock import AsyncMock, patch

import discord_webhook
from discord_webhook import WebhookSender
from tests.test_publication_tracking import make_settings, workspace_tempdir

URL = "https://discord.com/api/webhooks/1/secret-token"


def http_error(code, body=b""):
    return urllib.error.HTTPError(URL, code, "error", {}, io.BytesIO(body))


class WebhookSenderTests(unittest.TestCase):
    def send(self, *responses):
        with patch.object(discord_webhook.urllib.request, "urlopen", side_effect=list(responses)) as urlopen, \
                patch.object(discord_webhook.time, "sleep") as sleep, \
                patch.object(discord_webhook.asyncio, "sleep", new=AsyncMock()):
            delivered = asyncio.run(WebhookSender(URL)("Hello <@42>"))
        return delivered, urlopen, sleep

    def test_posts_content_and_only_allows_user_mentions(self):
        delivered, urlopen, _ = self.send(io.BytesIO(b"{}"))

        self.assertTrue(delivered)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, f"{URL}?wait=true")
        body = json.loads(request.data)
        self.assertEqual(body["content"], "Hello <@42>")
        self.assertEqual(body["allowed_mentions"], {"parse": ["users"]})
        self.assertIn("DiscordBot", request.get_header("User-agent"))

    def test_retries_once_after_rate_limit(self):
        delivered, urlopen, sleep = self.send(http_error(429, b'{"retry_after": 2.5}'), io.BytesIO(b"{}"))

        self.assertTrue(delivered)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2.5)

    def test_rejected_message_returns_false_without_logging_url(self):
        with self.assertLogs("DiscordWebhook", level="ERROR") as logs:
            delivered, _, _ = self.send(http_error(404, b'{"message": "Unknown Webhook"}'))

        self.assertFalse(delivered)
        self.assertNotIn("secret-token", "\n".join(logs.output))


class WebhookSelectionTests(unittest.TestCase):
    def test_test_channel_uses_test_webhook(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir, discord_webhook_url="main", discord_test_webhook_url="test")
            self.assertEqual(settings.webhook_url, "main")
            settings.use_test_channel = True
            self.assertEqual(settings.webhook_url, "test")

    def test_no_webhook_configured_gives_no_url(self):
        with workspace_tempdir() as tmpdir:
            self.assertIsNone(make_settings(tmpdir).webhook_url)


if __name__ == "__main__":
    unittest.main()
