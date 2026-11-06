import contextlib
import io
import sys
import types
import unittest
from unittest.mock import patch

from settings import _parse_args, load_settings


def parse(*argv):
    with patch("sys.argv", ["bot.py", *argv]):
        return _parse_args()


class FlagTests(unittest.TestCase):
    def test_defaults(self):
        args = parse()
        self.assertEqual(args.days, 7)
        self.assertFalse(args.rss or args.api or args.nosave or args.nosend)

    def test_invalid_combinations_are_rejected(self):
        for argv in (["--rss", "--api"], ["--nomembersync", "--forcemembersync"], ["--days", "0"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                parse(*argv)


class LoadSettingsTests(unittest.TestCase):
    def load(self, *argv):
        fake_config = types.SimpleNamespace(
            DISCORD_WEBHOOK_URL="https://example.invalid/main",
            DISCORD_TEST_WEBHOOK_URL="https://example.invalid/test",
        )
        with patch("sys.argv", ["bot.py", *argv]), patch.dict(sys.modules, {"config": fake_config}), \
                patch("settings.load_tracked_authors"):
            return load_settings()

    def test_normal_run_saves(self):
        self.assertFalse(self.load().no_save)

    def test_test_channel_run_never_saves(self):
        # Saving would mark papers as announced that never reached the real channel.
        settings = self.load("--testchannel")
        self.assertTrue(settings.no_save)
        self.assertEqual(settings.webhook_url, "https://example.invalid/test")


if __name__ == "__main__":
    unittest.main()
