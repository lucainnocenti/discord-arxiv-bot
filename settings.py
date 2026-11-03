# settings.py
import argparse
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import datetime
from zoneinfo import ZoneInfo
import logging

import config  # Your existing config file
from tracked_authors import expand_discord_ids, expand_target_names, load_tracked_authors

# --- Constants ---
DEFAULT_CATEGORY = "quant-ph"
DEFAULT_MAX_RESULTS = 1000
DEFAULT_SOURCE = "rss"
API_SOURCE = "api"
RSS_SOURCE = "rss"
SOURCES = [API_SOURCE, RSS_SOURCE]

EASTERN_TZ = ZoneInfo('America/New_York')

@dataclass
class AppSettings:
    """Holds all application settings."""
    discord_token: str
    channel_id: int
    test_channel_id: int
    target_authors: List[str]
    author_discord_ids: Dict[str, int]
    crossref_mailto: Optional[str] = None

    # File Paths (consider making these configurable too)
    script_dir: str = field(default_factory=lambda: os.path.dirname(os.path.abspath(__file__)))
    log_path: str = field(init=False)
    last_submission_file: str = field(init=False)
    last_rss_check_file: str = field(init=False)
    posted_papers_file: str = field(init=False)
    tracked_authors_file: str = field(init=False)
    last_member_sync_file: str = field(init=False)

    # Runtime Flags from CLI
    no_save: bool = False
    no_send: bool = False
    last_date_override: Optional[datetime] = None
    source: str = DEFAULT_SOURCE
    force_rss_check: bool = False
    use_test_channel: bool = False
    member_sync_enabled: bool = True
    members_page_url: Optional[str] = None
    force_member_sync: bool = False

    # Maintainer notifications (optional)
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # Fetching Params
    category: str = DEFAULT_CATEGORY
    max_results: int = DEFAULT_MAX_RESULTS

    def __post_init__(self):
        """Calculate path attributes after initialization."""
        self.log_path = os.path.join(self.script_dir, "bot.log")
        self.last_submission_file = os.path.join(self.script_dir, "last_submission_date.txt")
        self.last_rss_check_file = os.path.join(self.script_dir, "last_rss_check.txt")
        self.posted_papers_file = os.path.join(self.script_dir, "posted_papers.txt")
        self.tracked_authors_file = os.path.join(self.script_dir, "tracked_authors.json")
        self.last_member_sync_file = os.path.join(self.script_dir, "last_member_sync.txt")

        # Validation
        if self.source not in SOURCES:
             raise ValueError(f"Invalid source: {self.source}. Must be one of {SOURCES}")
        if self.source == RSS_SOURCE and self.last_date_override:
            raise ValueError("Custom lastdate override is not compatible with RSS source.")
        if self.source == API_SOURCE and self.force_rss_check:
             raise ValueError("--forcerss flag is not compatible with API source.")
        if self.force_member_sync and not self.member_sync_enabled:
             raise ValueError("--forcemembersync is not compatible with --nomembersync.")

    def reload_tracked_authors(self):
        """Re-reads tracked_authors.json, e.g. after the website sync added names."""
        tracked = load_tracked_authors(self.tracked_authors_file, legacy_config=config)
        self.target_authors = expand_target_names(tracked)
        self.author_discord_ids = expand_discord_ids(tracked, config.AUTHOR_DISCORD_IDS)

def load_settings() -> AppSettings:
    """Loads settings from config, env vars, and CLI args."""
    parser = argparse.ArgumentParser(description="ArXiv Discord Bot")
    parser.add_argument("--nosave", action="store_true", help="Prevent saving last check dates.")
    parser.add_argument("--nosend", action="store_true", help="Prevent sending messages to Discord.")
    parser.add_argument("--lastdate", type=str, help="Override last API check date (YYYY-MM-DDTHH:MM:SS).")
    parser.add_argument("--source", choices=SOURCES, default=DEFAULT_SOURCE, help="Data source (api or rss).")
    parser.add_argument("--forcerss", action="store_true", help="Force RSS check even if already done today.")
    parser.add_argument("--testchannel", action="store_true", help="Use the test Discord channel.")
    parser.add_argument("--nomembersync", action="store_true", help="Skip the weekly sync of new authors from the group website.")
    parser.add_argument("--forcemembersync", action="store_true", help="Run the website member sync even if it ran this week.")
    # Add more args if needed (e.g., --category, --max-results)

    args = parser.parse_args()

    last_date_override = None
    if args.lastdate:
        try:
            last_date_override = datetime.fromisoformat(args.lastdate)
            logging.info(f"Using command-line last date override: {last_date_override}")
        except ValueError as e:
            logging.error(f"Invalid date format for --lastdate: {e}. Exiting.")
            exit(1) # Or raise an exception

    # Consider loading secrets from environment variables for better security
    discord_token = os.getenv("DISCORD_TOKEN", config.DISCORD_TOKEN)
    if not discord_token:
         raise ValueError("DISCORD_TOKEN not found in config.py or environment variables.")

    settings = AppSettings(
        discord_token=discord_token,
        channel_id=config.CHANNEL_ID,
        test_channel_id=config.TEST_CHANNEL_ID,
        # Filled from tracked_authors.json by reload_tracked_authors() below.
        target_authors=[],
        author_discord_ids={},
        # Crossref recommends including contact info in API requests so they can
        # reach the maintainer if the bot causes issues or needs coordination.
        crossref_mailto=os.getenv("CROSSREF_MAILTO", getattr(config, "CROSSREF_MAILTO", None)),
        no_save=args.nosave,
        no_send=args.nosend,
        last_date_override=last_date_override,
        source=args.source.lower(),
        force_rss_check=args.forcerss,
        use_test_channel=args.testchannel,
        member_sync_enabled=not args.nomembersync,
        members_page_url=getattr(config, "MEMBERS_PAGE_URL", None),
        force_member_sync=args.forcemembersync,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", getattr(config, "TELEGRAM_BOT_TOKEN", None)),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", getattr(config, "TELEGRAM_CHAT_ID", None)),
        # category=args.category, # If added to argparse
        # max_results=args.max_results # If added to argparse
    )

    # Post-init validation will run here
    settings.reload_tracked_authors()

    return settings
