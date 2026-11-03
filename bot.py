"""Discord bot entrypoint: sets up logging and the run lock, then runs the
pipeline, posting through a channel webhook when one is configured and
otherwise through a short-lived bot login."""

import discord
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# Import the structured components
from settings import load_settings, AppSettings
from arxiv_source import ArxivSource
from crossref import CrossrefClient
from discord_webhook import WebhookSender
from member_sync import sync_members_from_website
from pipeline import Pipeline
from publication_check import PublicationChecker
from registry import PaperRegistry
from run_lock import LockHeldElsewhere, run_lock

LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 5

def setup_logging(log_path: str):
    """Configures logging to file and console."""
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
    # Silence overly verbose libraries if needed
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class ArxivBotClient(discord.Client):
    """Logs in, runs the pipeline against the target channel, and logs out."""

    def __init__(self, settings: AppSettings, pipeline_factory, **options):
        super().__init__(intents=discord.Intents.default(), **options)
        self.settings = settings
        self.pipeline_factory = pipeline_factory
        self.logger = logging.getLogger(self.__class__.__name__)

    async def on_ready(self):
        self.logger.info(f"Logged in as {self.user}")
        try:
            channel = self._target_channel()
            if channel is not None:
                await self.pipeline_factory(self._sender(channel)).run()
        except Exception as e:
            self.logger.exception(f"An error occurred during the bot run: {e}")
        finally:
            self.logger.info("Check complete. Closing bot connection.")
            await self.close()

    def _target_channel(self):
        channel_id = self.settings.test_channel_id if self.settings.use_test_channel else self.settings.channel_id
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            self.logger.error(f"Could not find specified TextChannel with ID: {channel_id}. Check configuration.")
            return None
        self.logger.info(f"Operating in channel: {channel.name} ({channel.id})")
        return channel

    def _sender(self, channel: discord.TextChannel):
        async def send(message: str) -> bool:
            try:
                await channel.send(message)
                await asyncio.sleep(1)
                return True
            except discord.errors.HTTPException as e:
                self.logger.error(f"Discord API error sending message: {e.status} {e.code} - {e.text}")
            except Exception as e:
                self.logger.exception(f"Unexpected error sending message: {e}")
            return False
        return send


async def run_bot():
    """Loads settings, sets up components, and starts the bot."""
    try:
        settings = load_settings() # Parse command line arguments once into a typed settings object.
        setup_logging(settings.log_path) # Setup logging early so downstream startup failures are captured.
        logging.info("Configuration loaded successfully.")
        logging.info(f"Running with lookback: {settings.lookback_days} days, Test Channel: {settings.use_test_channel}, No Save: {settings.no_save}, No Send: {settings.no_send}")
        if settings.ignored_flags:
            logging.warning(f"Ignoring retired flags {', '.join(settings.ignored_flags)}; papers are found with a rolling --days window now.")

        # Dry runs write no state, so they neither need nor take the lock.
        if settings.no_save:
            await _run(settings)
        else:
            with run_lock(settings.run_lock_file):
                await _run(settings)

    except LockHeldElsewhere as e:
        logging.warning(f"{e} Skipping this run.")
    except ValueError as e:
         logging.error(f"Configuration error: {e}")
         # No need to setup full logging if basic config fails
         print(f"Configuration error: {e}", file=sys.stderr)
         sys.exit(1)
    except discord.LoginFailure:
        logging.error("Discord login failed. Check the DISCORD_TOKEN.")
        print("Discord login failed. Check the DISCORD_TOKEN.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        logging.info("Shutdown requested via KeyboardInterrupt.")
    except Exception as e:
         # Catch-all for unexpected errors during setup or run
         logging.exception(f"An unexpected error occurred: {e}")
         print(f"An unexpected error occurred: {e}", file=sys.stderr)
         sys.exit(1)
    finally:
        logging.info("Bot process finished.")


async def _run(settings: AppSettings):
    """Runs one check: member sync, then the Discord session that posts papers."""
    # Weekly: pick up new group members before fetching so they count today.
    added_authors = await asyncio.to_thread(sync_members_from_website, settings)
    if added_authors:
        settings.reload_tracked_authors()

    registry = PaperRegistry.load(settings.posted_papers_file, read_only=settings.no_save)
    arxiv_source = ArxivSource(settings)
    publications = PublicationChecker(arxiv_source, CrossrefClient(settings.crossref_mailto))

    def pipeline_factory(send):
        return Pipeline(settings, registry, arxiv_source, publications, send)

    try:
        if settings.webhook_url:
            # A webhook needs no login: post straight through it.
            logging.info("Posting through the Discord webhook.")
            await pipeline_factory(WebhookSender(settings.webhook_url)).run()
        else:
            bot = ArxivBotClient(settings=settings, pipeline_factory=pipeline_factory)
            async with bot:
                await bot.start(settings.discord_token, reconnect=False)
    finally:
        # Covers runs that fail midway; a no-op when nothing changed.
        registry.save()


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logging.info("Shutdown requested via KeyboardInterrupt.")
