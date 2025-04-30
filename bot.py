# main.py (or bot.py)
import discord
import asyncio
import logging
import sys
from datetime import datetime
from typing import Set, Optional

# Import the structured components
from settings import load_settings, AppSettings, API_SOURCE, RSS_SOURCE
from state_manager import StateManager
from arxiv_fetcher import ArxivFetcher, Paper
from discord_formatter import format_paper_message

def setup_logging(log_path: str):
    """Configures logging to file and console."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout)
        ]
    )
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
        # Runtime set to track posted papers *within this run* - useful for RSS duplicates
        self.posted_in_this_run: Set[str] = set()
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
        """The main logic: fetch, filter, format, and post."""
        await self.wait_until_ready() # Ensure internal cache is ready

        target_channel_id = self.settings.test_channel_id if self.settings.use_test_channel else self.settings.channel_id
        channel = self.get_channel(target_channel_id)

        if not isinstance(channel, discord.TextChannel):
            self.logger.error(f"Could not find specified TextChannel with ID: {target_channel_id}. Check configuration.")
            return
        self.logger.info(f"Operating in channel: {channel.name} ({channel.id})")

        # --- Fetching ---
        last_api_check_time = self.state_manager.get_last_api_check_time()

        # Conditional RSS check based on date
        if self.settings.source == RSS_SOURCE and self.state_manager.has_checked_rss_today():
             self.logger.info("RSS source selected, but already checked today. No fetch needed.")
             papers_to_post = []
        else:
            try:
                # Pass the relevant date only if using API source
                papers_to_post = await self.fetcher.fetch_latest_papers(last_api_check_time)
            except Exception as e:
                 self.logger.exception(f"Failed to fetch papers: {e}")
                 papers_to_post = [] # Ensure it's an empty list on fetch failure

        if not papers_to_post:
            self.logger.info("No new papers found matching criteria.")
            # Still need to potentially update RSS check time even if no papers found
            if self.settings.source == RSS_SOURCE and not self.settings.force_rss_check:
                 # Save RSS check time if we performed a check (i.e., didn't skip)
                 self.state_manager.save_rss_check_time()
            return

        # --- Processing and Posting ---
        papers_posted_count = 0
        latest_paper_time: Optional[datetime] = None # Keep track for saving API state

        for paper in papers_to_post:
            # Use paper.id which should be the unique arXiv identifier (e.g., 'http://arxiv.org/abs/...')
            if paper.id in self.posted_in_this_run:
                self.logger.info(f"Paper {paper.id} already processed in this run, skipping.")
                continue

            self.logger.info(f"Processing paper: '{paper.title}' ({paper.id})")

            # Format message
            message = format_paper_message(paper, self.settings)

            if message:
                if self.settings.no_send:
                    self.logger.info(f"[NO_SEND] Would post message for paper: {paper.title}")
                    self.logger.debug(f"Message content:\n{message}") # Log message if not sending
                else:
                    try:
                        self.logger.info(f"Sending message for paper: {paper.title}")
                        await channel.send(message)
                        papers_posted_count += 1
                        await asyncio.sleep(1) # Small delay between messages
                    except discord.errors.HTTPException as e:
                        self.logger.error(f"Discord API error sending message for '{paper.title}': {e.status} {e.code} - {e.text}")
                    except Exception as e:
                         self.logger.exception(f"Unexpected error sending message for '{paper.title}': {e}")

                # Mark as processed for this run regardless of send success (prevents retries in same run)
                self.posted_in_this_run.add(paper.id)

                # Track the latest published time for API state saving
                # Use timezone-naive comparison if needed, or ensure consistency
                # Assuming paper.published is timezone-aware (UTC from API/RSS parser)
                # Convert last_api_check_time to aware UTC if it's naive
                if self.settings.source == API_SOURCE:
                     paper_published_naive = paper.published.replace(tzinfo=None)
    
                     if latest_paper_time is None:
                          latest_paper_time = paper.published
                     else:
                          current_latest = latest_paper_time.replace(tzinfo=None)
                          if paper_published_naive > current_latest:
                               latest_paper_time = paper.published # Keep the original aware object

            else:
                self.logger.warning(f"Skipping paper '{paper.title}' because message formatting failed (likely too long).")
                self.posted_in_this_run.add(paper.id) # Also mark as processed to avoid retrying


        self.logger.info(f"Finished processing. Posted {papers_posted_count} new paper notifications.")

        # --- State Saving ---
        # Only save state if papers were actually processed or checked
        if self.settings.source == API_SOURCE and latest_paper_time:
            # Save the timestamp of the *latest* paper found in this batch
             self.logger.info(f"Latest paper time found for API source: {latest_paper_time}")
             self.state_manager.save_last_api_check_time(latest_paper_time)
        elif self.settings.source == RSS_SOURCE:
             # Save RSS check time if we performed a check (didn't skip due to already checked)
             # This covers cases where papers were found or where the check ran but found nothing new.
             if not self.state_manager.has_checked_rss_today() or self.settings.force_rss_check:
                 self.state_manager.save_rss_check_time()

async def run_bot():
    """Loads settings, sets up components, and starts the bot."""
    try:
        settings = load_settings()
        setup_logging(settings.log_path) # Setup logging early
        logging.info("Configuration loaded successfully.")
        logging.info(f"Running with source: {settings.source}, Test Channel: {settings.use_test_channel}, No Save: {settings.no_save}, No Send: {settings.no_send}")

        state_manager = StateManager(settings)
        fetcher = ArxivFetcher(settings)
        # Formatter is functional, no class needed unless it grows state

        bot = ArxivBotClient(settings=settings, state_manager=state_manager, fetcher=fetcher)

        await bot.start(settings.discord_token)

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


if __name__ == "__main__":
    asyncio.run(run_bot())