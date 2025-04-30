# settings.py
import argparse
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import datetime
from zoneinfo import ZoneInfo
import logging

import config  # Your existing config file

# --- Constants ---
DEFAULT_CATEGORY = "quant-ph"
DEFAULT_MAX_RESULTS = 50
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

    # File Paths (consider making these configurable too)
    script_dir: str = field(default_factory=lambda: os.path.dirname(os.path.abspath(__file__)))
    log_path: str = field(init=False)
    last_submission_file: str = field(init=False)
    last_rss_check_file: str = field(init=False)

    # Runtime Flags from CLI
    no_save: bool = False
    no_send: bool = False
    last_date_override: Optional[datetime] = None
    source: str = DEFAULT_SOURCE
    force_rss_check: bool = False
    use_test_channel: bool = False

    # Fetching Params
    category: str = DEFAULT_CATEGORY
    max_results: int = DEFAULT_MAX_RESULTS

    def __post_init__(self):
        """Calculate path attributes after initialization."""
        self.log_path = os.path.join(self.script_dir, "bot.log")
        self.last_submission_file = os.path.join(self.script_dir, "last_submission_date.txt")
        self.last_rss_check_file = os.path.join(self.script_dir, "last_rss_check.txt")

        # Validation
        if self.source not in SOURCES:
             raise ValueError(f"Invalid source: {self.source}. Must be one of {SOURCES}")
        if self.source == RSS_SOURCE and self.last_date_override:
            raise ValueError("Custom lastdate override is not compatible with RSS source.")
        if self.source == API_SOURCE and self.force_rss_check:
             raise ValueError("--forcerss flag is not compatible with API source.")

def load_settings() -> AppSettings:
    """Loads settings from config, env vars, and CLI args."""
    parser = argparse.ArgumentParser(description="ArXiv Discord Bot")
    parser.add_argument("--nosave", action="store_true", help="Prevent saving last check dates.")
    parser.add_argument("--nosend", action="store_true", help="Prevent sending messages to Discord.")
    parser.add_argument("--lastdate", type=str, help="Override last API check date (YYYY-MM-DDTHH:MM:SS).")
    parser.add_argument("--source", choices=SOURCES, default=DEFAULT_SOURCE, help="Data source (api or rss).")
    parser.add_argument("--forcerss", action="store_true", help="Force RSS check even if already done today.")
    parser.add_argument("--testchannel", action="store_true", help="Use the test Discord channel.")
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
        target_authors=config.TARGET_AUTHORS,
        author_discord_ids=config.AUTHOR_DISCORD_IDS,
        no_save=args.nosave,
        no_send=args.nosend,
        last_date_override=last_date_override,
        source=args.source.lower(),
        force_rss_check=args.forcerss,
        use_test_channel=args.testchannel,
        # category=args.category, # If added to argparse
        # max_results=args.max_results # If added to argparse
    )

    # Post-init validation will run here

    return settings