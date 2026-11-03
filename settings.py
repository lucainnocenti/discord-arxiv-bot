"""Runtime settings: private values from config.py (or environment variables),
tracked authors from tracked_authors.json, and command-line flags."""

import argparse
import os
import socket
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tracked_authors import expand_discord_ids, expand_target_names, load_tracked_authors

DEFAULT_CATEGORY = "quant-ph"
DEFAULT_MAX_RESULTS = 1000
# arXiv announces papers up to ~4 days after submission (weekends, holidays,
# skipped mailings), so a week-long window leaves a comfortable margin.
DEFAULT_LOOKBACK_DAYS = 7


@dataclass
class AppSettings:
    """Holds all application settings."""
    # Filled from tracked_authors.json by reload_tracked_authors().
    target_authors: List[str] = field(default_factory=list)
    author_discord_ids: Dict[str, int] = field(default_factory=dict)

    # Private values (config.py or environment)
    discord_webhook_url: Optional[str] = None
    discord_test_webhook_url: Optional[str] = None
    crossref_mailto: Optional[str] = None
    members_page_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # File paths, all next to the bot
    script_dir: str = field(default_factory=lambda: os.path.dirname(os.path.abspath(__file__)))
    log_path: str = field(init=False)
    run_lock_file: str = field(init=False)
    posted_papers_file: str = field(init=False)
    tracked_authors_file: str = field(init=False)
    last_member_sync_file: str = field(init=False)

    # Command-line flags
    no_save: bool = False
    no_send: bool = False
    use_test_channel: bool = False
    member_sync_enabled: bool = True
    force_member_sync: bool = False
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    # Read only the RSS feed instead of the API search (normally just a fallback).
    rss_only: bool = False

    # Fetching parameters
    category: str = DEFAULT_CATEGORY
    max_results: int = DEFAULT_MAX_RESULTS

    def __post_init__(self):
        # One log per machine: the folder is synced between computers, and a
        # single shared log file gets forked or dropped by the sync client.
        self.log_path = os.path.join(self.script_dir, "logs", f"bot-{socket.gethostname()}.log")
        self.run_lock_file = os.path.join(self.script_dir, "run.lock")
        self.posted_papers_file = os.path.join(self.script_dir, "posted_papers.txt")
        self.tracked_authors_file = os.path.join(self.script_dir, "tracked_authors.json")
        self.last_member_sync_file = os.path.join(self.script_dir, "last_member_sync.txt")

        if self.lookback_days < 1:
            raise ValueError("--days must be at least 1.")
        if self.force_member_sync and not self.member_sync_enabled:
            raise ValueError("--forcemembersync is not compatible with --nomembersync.")

    @property
    def webhook_url(self) -> Optional[str]:
        """The webhook for the channel this run posts to."""
        return self.discord_test_webhook_url if self.use_test_channel else self.discord_webhook_url

    def reload_tracked_authors(self):
        """Re-reads tracked_authors.json, e.g. after the website sync added names."""
        tracked = load_tracked_authors(self.tracked_authors_file)
        self.target_authors = expand_target_names(tracked)
        self.author_discord_ids = expand_discord_ids(tracked)


def load_settings() -> AppSettings:
    """Builds settings from command-line flags, config.py and the environment."""
    parser = argparse.ArgumentParser(description="Posts new and newly published arXiv papers by tracked authors to Discord.")
    parser.add_argument("--nosave", action="store_true", help="Don't write the paper registry or other state.")
    parser.add_argument("--nosend", action="store_true", help="Don't post anything to Discord.")
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS, help=f"Look for papers submitted in the last N days (default {DEFAULT_LOOKBACK_DAYS}).")
    parser.add_argument("--rss", action="store_true", help="Use the arXiv RSS feed (latest announcement) instead of the API search; normally the feed is only a fallback.")
    parser.add_argument("--testchannel", action="store_true", help="Post to the test channel's webhook.")
    parser.add_argument("--nomembersync", action="store_true", help="Skip the weekly sync of new authors from the group website.")
    parser.add_argument("--forcemembersync", action="store_true", help="Run the website member sync even if it ran this week.")
    args = parser.parse_args()

    # Imported here so the rest of the code (and the tests) work without a
    # private config.py; see config.example.py.
    import config

    def private(name: str) -> Optional[str]:
        return os.getenv(name) or getattr(config, name, None)

    settings = AppSettings(
        discord_webhook_url=private("DISCORD_WEBHOOK_URL"),
        discord_test_webhook_url=private("DISCORD_TEST_WEBHOOK_URL"),
        # Crossref recommends including contact info in API requests so they can
        # reach the maintainer if the bot causes issues or needs coordination.
        crossref_mailto=private("CROSSREF_MAILTO"),
        members_page_url=private("MEMBERS_PAGE_URL"),
        telegram_bot_token=private("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=private("TELEGRAM_CHAT_ID"),
        no_save=args.nosave,
        no_send=args.nosend,
        use_test_channel=args.testchannel,
        member_sync_enabled=not args.nomembersync,
        force_member_sync=args.forcemembersync,
        lookback_days=args.days,
        rss_only=args.rss,
    )
    if not settings.webhook_url and not settings.no_send:
        name = "DISCORD_TEST_WEBHOOK_URL" if settings.use_test_channel else "DISCORD_WEBHOOK_URL"
        raise ValueError(f"{name} is not set in config.py; the bot has nowhere to post.")

    settings.reload_tracked_authors()
    return settings
