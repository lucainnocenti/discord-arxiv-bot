"""Entry point: one run of the bot.

Sets up logging and the run lock, syncs new group members weekly, then runs
the pipeline, which posts to Discord through the channel webhook.
"""

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from abstract_shortener import AbstractShortener
from arxiv_api import ArxivApi
from crossref import CrossrefClient
from discord_formatter import discord_length
from discord_webhook import WebhookSender
from member_sync import sync_members_from_website
from new_papers import NewPaperFinder
from pipeline import Pipeline
from publication_check import PublicationChecker
from registry import PaperRegistry
from run_lock import LockHeldElsewhere, run_lock
from settings import AppSettings, load_settings

LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 5


def setup_logging(log_path: str):
    """Logs to the console and to a rotating per-machine file."""
    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Remove any handlers that libraries (e.g. arxiv) may have added before
    # this function runs — basicConfig() would be a no-op otherwise.
    root.handlers.clear()
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    # Rotate so the log never grows unbounded: ~1 MB per file, 5 old files kept.
    fh = RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


async def run_bot() -> int:
    """Runs the bot once; returns the process exit code."""
    try:
        settings = load_settings()
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    setup_logging(settings.log_path)
    logging.info(
        f"Starting run: lookback {settings.lookback_days} days, test channel: {settings.use_test_channel}, "
        f"no save: {settings.no_save}, no send: {settings.no_send}"
    )
    try:
        # Dry runs write no state, so they neither need nor take the lock.
        if settings.no_save:
            await _run(settings)
        else:
            with run_lock(settings.run_lock_file):
                await _run(settings)
        return 0
    except LockHeldElsewhere as e:
        logging.warning(f"{e} Skipping this run.")
        return 0
    except Exception as e:
        logging.exception(f"Run failed: {e}")
        return 1
    finally:
        logging.info("Bot process finished.")


async def _run(settings: AppSettings):
    # Weekly: pick up new group members before fetching so they count today.
    if await asyncio.to_thread(sync_members_from_website, settings):
        settings.reload_tracked_authors()

    registry = PaperRegistry.load(settings.posted_papers_file, read_only=settings.no_save)
    api = ArxivApi()
    new_papers = NewPaperFinder(settings, api)
    publications = PublicationChecker(api, CrossrefClient(settings.crossref_mailto))
    send = WebhookSender(settings.webhook_url) if settings.webhook_url else None
    if settings.anthropic_api_key:
        shortener = AbstractShortener(settings.anthropic_api_key, measure=discord_length)
    else:
        shortener = None
        logging.info("ANTHROPIC_API_KEY is not set: abstracts too long for a message will be truncated.")
    try:
        await Pipeline(settings, registry, new_papers, publications, send, shortener).run()
    finally:
        # Covers runs that fail midway; a no-op when nothing changed.
        registry.save()


if __name__ == "__main__":
    sys.exit(asyncio.run(run_bot()))
