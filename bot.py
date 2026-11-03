"""Discord bot entrypoint for fetching, formatting, and posting arXiv updates."""

import discord
import asyncio
import logging
import os
import sys
from datetime import date
from logging.handlers import RotatingFileHandler
from typing import Dict, Optional, Set

# Import the structured components
from settings import load_settings, AppSettings
from state_manager import PaperRecord, StateManager
from arxiv_fetcher import ArxivFetcher, Paper, TrackedPaperMetadata
from discord_formatter import format_paper_message
from member_sync import sync_members_from_website
from publication_schedule import is_crossref_check_due
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
    # Remove any handlers that libraries (e.g. arxiv, feedparser) may have
    # added before this function runs — basicConfig() would be a no-op otherwise.
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
    # logging.getLogger("pylatexenc").setLevel(logging.WARNING) # If it becomes noisy


class ArxivBotClient(discord.Client):
    def __init__(self, settings: AppSettings, state_manager: StateManager, fetcher: ArxivFetcher, **options):
        super().__init__(intents=discord.Intents.default(), **options)
        self.settings = settings
        self.state_manager = state_manager
        self.fetcher = fetcher
        # The registry is the bot's durable memory: whether a paper was already
        # announced as a preprint, and whether its later journal publication was
        # already announced too.
        self.paper_registry: Dict[str, PaperRecord] = self.state_manager.get_paper_registry()
        self.posted_in_this_run: Set[str] = set()
        self.publication_updates_in_this_run: Set[str] = set()
        self.logger = logging.getLogger(self.__class__.__name__) # Specific logger

    async def on_ready(self):
        """Called when the bot is ready."""
        self.logger.info(f"Logged in as {self.user}")
        try:
            await self.check_and_post_papers()
        except Exception as e:
            self.logger.exception(f"An error occurred during the check_and_post_papers routine: {e}")
        finally:
            self.logger.info("Check complete. Closing bot connection.")
            await self.close()

    async def check_and_post_papers(self):
        """The main logic: fetch, filter, format, post, then check for publication updates."""
        await self.wait_until_ready() # Ensure internal cache is ready

        # Resolve the destination once up front so the rest of the run can treat
        # "post to Discord" as a single target regardless of test/prod mode.
        target_channel_id = self.settings.test_channel_id if self.settings.use_test_channel else self.settings.channel_id
        channel = self.get_channel(target_channel_id)

        if not isinstance(channel, discord.TextChannel):
            self.logger.error(f"Could not find specified TextChannel with ID: {target_channel_id}. Check configuration.")
            return
        self.logger.info(f"Operating in channel: {channel.name} ({channel.id})")

        # --- Fetching ---
        # Every run looks at a rolling window of recent submissions; the
        # registry below decides what is actually new, so no cursor is needed.
        try:
            papers_to_post = await self.fetcher.fetch_latest_papers()
        except Exception as e:
            self.logger.exception(f"Failed to fetch papers: {e}")
            papers_to_post = [] # Ensure it's an empty list on fetch failure

        # --- Processing and Posting ---
        # This first loop handles brand-new preprints only. Publication updates
        # for already-known papers are handled in a second pass below.
        papers_posted_count = 0
        publication_updates_count = 0
        publication_check_degraded = False

        for paper in papers_to_post:
            if paper.id in self.posted_in_this_run:
                self.logger.info(f"Paper {paper.id} already processed in this run, skipping.")
                continue

            # The fetch window overlaps earlier runs, so most papers here were
            # already posted; check that before spending any Crossref calls.
            paper_record = self.paper_registry.get(paper.id, PaperRecord())
            if paper_record.posted:
                self.logger.info(f"Paper {paper.id} was already posted in a previous run, skipping.")
                self.posted_in_this_run.add(paper.id)
                continue

            # Enrich before posting so the initial announcement already
            # includes DOI/journal data when arXiv or Crossref has it.
            paper = await self.fetcher.enrich_publication_metadata(paper)

            self.logger.info(f"Processing paper: '{paper.title}' ({paper.id})")

            send_result = await self._send_paper_message(channel, paper, event_type="new")
            self.posted_in_this_run.add(paper.id)

            if send_result is True:
                papers_posted_count += 1
                record = self._update_paper_record(
                    paper,
                    posted=True,
                    published=bool(paper.journal_ref or paper.doi),
                )
                self.paper_registry[paper.id] = record
            elif send_result is False:
                self.logger.warning(f"Skipping paper '{paper.title}' because message formatting failed (likely too long).")
                self.posted_in_this_run.add(paper.id)

        # Second pass: revisit already-posted papers that still look
        # unpublished and emit a shorter "now published" message if needed.
        metadata_hydrated_count, metadata_hydration_degraded = await self._hydrate_incomplete_paper_records()
        publication_updates_count, publication_check_degraded = await self._check_for_publication_updates(channel)
        publication_check_degraded = publication_check_degraded or metadata_hydration_degraded

        if papers_posted_count == 0 and publication_updates_count == 0:
            if publication_check_degraded:
                self.logger.warning(
                    "No new papers were posted, and publication checks were incomplete because "
                    "arXiv metadata refresh was throttled or temporarily unavailable."
                )
            else:
                self.logger.info("No new papers or publication updates found matching criteria.")
        else:
            self.logger.info(
                f"Finished processing. Posted {papers_posted_count} new paper notifications and "
                f"{publication_updates_count} publication updates; hydrated metadata for "
                f"{metadata_hydrated_count} tracked records."
            )
            if publication_check_degraded:
                self.logger.warning(
                    "Publication checks completed with gaps because some tracked papers "
                    "could not be refreshed from arXiv this run."
                )

    async def _hydrate_incomplete_paper_records(self) -> tuple[int, bool]:
        """Backfills title/author metadata for legacy or sparse registry records."""
        candidate_ids = [
            paper_id
            for paper_id, record in self.paper_registry.items()
            if record.posted and (not record.title or not record.authors)
        ]
        if not candidate_ids:
            self.logger.info("No tracked paper records need metadata hydration.")
            return 0, False

        self.logger.info(f"Hydrating metadata for {len(candidate_ids)} incomplete tracked paper records.")
        try:
            refresh_result = await self.fetcher.fetch_papers_by_ids(candidate_ids)
        except Exception as e:
            self.logger.exception(f"Failed to hydrate tracked paper metadata: {e}")
            return 0, True

        hydrated_count = 0
        for paper in refresh_result.papers:
            current_record = self.paper_registry.get(paper.id, PaperRecord())
            if current_record.title and current_record.authors:
                continue

            record = self.state_manager.upsert_paper_record(
                paper.id,
                posted=current_record.posted,
                published=current_record.published,
                doi=paper.doi,
                journal_ref=paper.journal_ref,
                title=paper.title,
                authors=paper.authors,
            )
            self.paper_registry[paper.id] = record
            hydrated_count += 1

        if refresh_result.degraded:
            self.logger.warning(
                "Metadata hydration was partial: refreshed %d/%d tracked papers; "
                "%d were deferred to a future run.",
                len(refresh_result.papers),
                len(candidate_ids),
                len(refresh_result.failed_ids),
            )

        return hydrated_count, refresh_result.degraded

    async def _check_for_publication_updates(self, channel: discord.TextChannel) -> tuple[int, bool]:
        """Checks whether already-posted arXiv papers now have publication metadata."""
        # Only revisit papers that have already been announced in Discord but
        # have not yet been marked as published in the registry.
        tracked_records = {
            paper_id: record
            for paper_id, record in self.paper_registry.items()
            if record.posted and not record.published
        }
        tracked_ids = list(tracked_records)

        if not tracked_ids:
            self.logger.info("No tracked unpublished papers require publication checks.")
            return 0, False

        today = date.today()
        crossref_due_ids = {
            paper_id
            for paper_id, record in tracked_records.items()
            if is_crossref_check_due(paper_id, record.crossref_checked, today)
        }
        self.logger.info(
            f"Checking publication status for {len(tracked_ids)} tracked papers "
            f"(Crossref lookup due for {len(crossref_due_ids)})."
        )
        try:
            refresh_result = await self.fetcher.fetch_publication_updates(
                tracked_ids,
                tracked_metadata={
                    paper_id: TrackedPaperMetadata(
                        title=record.title,
                        authors=list(record.authors),
                        doi=record.doi,
                        journal_ref=record.journal_ref,
                    )
                    for paper_id, record in tracked_records.items()
                },
                crossref_due_ids=crossref_due_ids,
            )
        except Exception as e:
            self.logger.exception(f"Failed to refresh publication metadata: {e}")
            return 0, True

        if refresh_result.crossref_checked_ids:
            # Restart each looked-up paper's schedule; one registry write for all.
            self.paper_registry = self.state_manager.mark_crossref_checked(
                refresh_result.crossref_checked_ids, today
            )

        if refresh_result.degraded:
            self.logger.warning(
                "Publication metadata refresh was partial: checked %d/%d tracked papers; "
                "%d were deferred to a future run.",
                len(refresh_result.checked_ids),
                len(tracked_ids),
                len(refresh_result.failed_ids),
            )

        updates_sent = 0
        for paper in refresh_result.updates:
            if paper.id in self.publication_updates_in_this_run:
                continue

            # Re-read the current record in case earlier work in this same run
            # already flipped the publication flag.
            current_record = self.paper_registry.get(paper.id, PaperRecord())
            if not current_record.posted or current_record.published:
                continue

            # Once a publication update is successfully sent, the registry is
            # flipped to published=True so the message is emitted only once.
            send_result = await self._send_paper_message(channel, paper, event_type="published")
            self.publication_updates_in_this_run.add(paper.id)
            if send_result is not True:
                continue

            updates_sent += 1
            record = self._update_paper_record(paper, published=True)
            self.paper_registry[paper.id] = record

        return updates_sent, refresh_result.degraded

    async def _send_paper_message(self, channel: discord.TextChannel, paper: Paper, *, event_type: str) -> Optional[bool]:
        """Formats and sends a Discord message.

        Returns True when a message was delivered, False when formatting failed, and
        None during --nosend dry runs.
        """
        # Keep formatting separate from transport so long-message failures are
        # caught before we call the Discord API.
        message = format_paper_message(paper, self.settings, event_type=event_type)
        if not message:
            return False

        if self.settings.no_send:
            self.logger.info(f"[NO_SEND] Would post {event_type} message for paper: {paper.title}")
            self.logger.debug(f"Message content:\n{message}")
            return None

        try:
            self.logger.info(f"Sending {event_type} message for paper: {paper.title}")
            await channel.send(message)
            await asyncio.sleep(1)
            return True
        except discord.errors.HTTPException as e:
            self.logger.error(f"Discord API error sending message for '{paper.title}': {e.status} {e.code} - {e.text}")
        except Exception as e:
            self.logger.exception(f"Unexpected error sending message for '{paper.title}': {e}")
        return False

    def _update_paper_record(self, paper: Paper, *, posted: Optional[bool] = None, published: Optional[bool] = None) -> PaperRecord:
        """Persists tracked-paper state and returns the updated in-memory record."""
        # Keep the registry updates centralized so both the in-memory state and
        # the on-disk JSON-lines file stay in sync.
        record = self.state_manager.upsert_paper_record(
            paper.id,
            posted=posted,
            published=published,
            doi=paper.doi,
            journal_ref=paper.journal_ref,
            title=paper.title,
            authors=paper.authors,
        )
        self.paper_registry[paper.id] = record
        return record

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
    # The runtime is deliberately split into state, fetch, and Discord
    # layers so each concern can be tested or changed independently.
    state_manager = StateManager(settings)

    # Weekly: pick up new group members before fetching so they count today.
    added_authors = await asyncio.to_thread(sync_members_from_website, settings)
    if added_authors:
        settings.reload_tracked_authors()

    fetcher = ArxivFetcher(settings)
    bot = ArxivBotClient(settings=settings, state_manager=state_manager, fetcher=fetcher)

    async with bot:
        await bot.start(settings.discord_token, reconnect=False)


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logging.info("Shutdown requested via KeyboardInterrupt.")
