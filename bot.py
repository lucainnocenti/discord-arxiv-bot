import discord          # Discord library to interact with the Discord API
import asyncio          # For asynchronous event handling and scheduling tasks
import arxiv            # For querying the arXiv API for research papers
import os               # For file path and OS interactions
import sys              # For system-specific parameters and functions
import logging          # For logging messages to both console and file
import config           # Module to import configuration data such as tokens and IDs
from datetime import datetime, timedelta  # For date and time handling
import feedparser
from typing import List, Dict, Any

# ----------------------------------------------------------------------------
# File and Directory Setup
# ----------------------------------------------------------------------------
# Determine the directory where the current script is located.
script_dir = os.path.dirname(os.path.abspath(__file__))

# Build the absolute path for the log file.
log_path = os.path.join(script_dir, "bot.log")
# Define the file that stores the last time arXiv was checked.
LAST_SUBMISSION_FILE = os.path.join(script_dir, "last_submission_date.txt")

# ----------------------------------------------------------------------------
# Logging Setup
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,  # Log INFO, WARNING, ERROR and above
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_path),    # Log to file
        logging.StreamHandler(sys.stdout)   # Also log to console
    ]
)

# ----------------------------------------------------------------------------
# Configuration and Constants
# ----------------------------------------------------------------------------
DISCORD_TOKEN = config.DISCORD_TOKEN   # Bot's Discord authentication token
CHANNEL_ID = config.CHANNEL_ID         # Channel ID where messages are sent
TARGET_AUTHORS = config.TARGET_AUTHORS  # List of target authors
AUTHOR_DISCORD_IDS = config.AUTHOR_DISCORD_IDS  # Mapping from author names to their Discord user IDs.



# Use a set to track posted papers during runtime.
posted_papers = set()

def get_latest_papers_from_rss(category: str = "quant-ph", max_results: int = 100) -> List[Dict[str, Any]]:
    """
    Fetches latest papers from arXiv RSS feed for a given category.
    
    Args:
        category: arXiv category (default: "quant-ph")
        max_results: Maximum number of results to return (default: 100)
    
    Returns:
        List of dictionaries containing paper details with keys:
        - title: Title of the paper
        - authors: List of author names
        - published: Publication date (string)
        - updated: Last updated date (string)
        - summary: Abstract of the paper
        - link: URL to the paper
        - pdf_link: Direct link to PDF (if available)
        - journal_ref: Journal reference (if available)
    """

    # Parse the RSS feed
    feed_url = f"http://rss.arxiv.org/rss/{category}"
    feed = feedparser.parse(feed_url)

    # Process results
    papers = []
    for entry in feed.entries[:max_results]:
        # Extract authors
        authors = [author.get('name', '') for author in (entry.get('authors') or [])]
        
        # Create PDF link from the main link
        paper_id = entry.id.split('/abs/')[-1]
        pdf_link = f"http://arxiv.org/pdf/{paper_id}"

        # the rss times are in the format 'Mon, 24 Mar 2025 00:00:00 -0400'. Convert to datetime object
        entry.published = datetime.strptime(entry.published, '%a, %d %b %Y %H:%M:%S %z')
        
        paper = {
            'title': entry.title,
            'authors': authors,
            'published': entry.published,
            'summary': entry.summary,
            'type': entry.arxiv_announce_type,
            'link': entry.link,
            'pdf_link': pdf_link,
            'journal_ref': entry.get('arxiv_journal_reference', 'None')
        }
        
        papers.append(paper)
    
    return papers

# ----------------------------------------------------------------------------
# Helper Functions
# ----------------------------------------------------------------------------
def get_last_submission_date():
    """
    Reads the last checked date from a file, unless overridden by command line argument.
    """
    # use the date provided via command line if available.
    if LAST_DATE_OVERRIDE is not None:
        logging.info("Using override date provided via --lastdate argument")
        return LAST_DATE_OVERRIDE
    # otherwise read the date from the file
    if os.path.exists(LAST_SUBMISSION_FILE):
        try:
            with open(LAST_SUBMISSION_FILE, 'r') as f:
                date_str = f.read().strip()
                return datetime.fromisoformat(date_str)
        except Exception as e:
            logging.error(f"Error reading last check date: {e}")
            return datetime.now() - timedelta(days=1)
    else:
        newdate = datetime.now() - timedelta(days=1)
        with open(LAST_SUBMISSION_FILE, 'w') as f:
            f.write(newdate.isoformat())
        return newdate

def save_last_submission_time(time):
    """
    Saves the time given as argument in LAST_SUBMISSION_FILE.
    The time should be a datetime object.
    """
    try:
        with open(LAST_SUBMISSION_FILE, 'w') as f:
            f.write(time.isoformat())
        logging.info(f"Saved date {time.isoformat()} to {LAST_SUBMISSION_FILE}")
    except Exception as e:
        logging.error(f"Error saving last check date: {e}")

def build_arxiv_query(last_submission_date, target_authors):
    """
    Constructs an arXiv query string for 'quant-ph' papers by target authors
    submitted after last_submission_date.
    """
    authors_query = ' OR '.join(f'au:"{author}"' for author in target_authors)
    query = f'cat:quant-ph AND ({authors_query})'
    query += f' AND submittedDate:[{last_submission_date.strftime("%Y%m%d%H%M%S")} TO 99999999]'
    logging.info(f"Constructed combined query: {query}")
    return query

def build_target_authors_string(result_authors, target_authors, author_discord_ids):
    """
    Returns a human-friendly string with Discord tagging for target authors found in result_authors.
    Prioritizes the first author if they match.
    """
    # Identify target authors in the result while preserving the order defined in target_authors.
    target_in_result = [
        name for name in target_authors
        if any(name.lower() == author.lower() for author in result_authors)
    ]

    if not target_in_result:
        return "unknown"

    # Reorder so that if the first author is a target, they appear first.
    first_author = result_authors[0]
    for target in target_in_result:
        if target.lower() in first_author.lower():
            target_in_result.remove(target)
            target_in_result.insert(0, target)
            break

    # Build the string by tagging authors when a Discord ID is available.
    tagged_authors = [
        f"<@{author_discord_ids[author]}>" if author in author_discord_ids else author
        for author in target_in_result
    ]
    if len(tagged_authors) == 1:
        return tagged_authors[0]
    return ", ".join(tagged_authors[:-1]) + " and " + tagged_authors[-1]

# ----------------------------------------------------------------------------
# Discord Bot Class
# ----------------------------------------------------------------------------
class ArxivBot(discord.Client):
    async def on_ready(self):
        """
        Called when the bot has successfully connected to Discord.
        Logs the bot's identity, performs a one-time arXiv check, and then shuts down.
        """
        logging.info(f"Logged in as {self.user}")
        await self.check_arxiv_once()
        await self.close()  # Close after processing to avoid a long-running process

    async def check_arxiv_once(self):
        """
        Checks for new arXiv papers from target authors since the last check date,
        constructs messages, and posts them to a specified Discord channel.
        """
        await self.wait_until_ready()
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            logging.error("ERROR: Could not get channel. Please check CHANNEL_ID!")
            return

        logging.info(f"Posting to channel: {channel}")

        # Get last check date and construct query.
        last_submission_date = get_last_submission_date()
        logging.info(f"Checking papers published since: {last_submission_date}")
        query = build_arxiv_query(last_submission_date, TARGET_AUTHORS)
        
        # Set up the arXiv search
        search = arxiv.Search(
            query=query,
            max_results=50,  # Adjust maximum results if necessary
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Ascending,
        )
        logging.info("Performing search on arXiv...")

        try:
            # Use an executor to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, lambda: list(arxiv.Client().results(search)))
            logging.info(f"Found {len(results)} papers with the combined query.")

            papers_posted = 0
            for result in results:

                arxiv_id = result.entry_id
                if arxiv_id in posted_papers:
                    logging.info(f"Already posted '{result.title}', skipping.")
                    continue

                # Extract authors from the result.
                result_authors = [a.name for a in result.authors]

                # Build a string with target authors and Discord tags.
                target_authors_str = build_target_authors_string(result_authors, TARGET_AUTHORS, AUTHOR_DISCORD_IDS)

                # Mark the paper as posted.
                posted_papers.add(arxiv_id)

                # Extract relevant information from the result.
                title = result.title
                link = result.entry_id  # Typically the paper URL
                summary = result.summary
                journal_ref = result.journal_ref

                # if summary is too long, truncate it to 1000 characters
                if len(summary) > 1400:
                    summary = summary[:1400] + '[...]'

                published_str = result.published.strftime('%Y-%m-%d %H:%M:%S')
                logging.info(f"Found new paper: '{title}' by {target_authors_str}, submitted on {published_str}.")

                # Check if the submission date is the latest among the papers found.
                if result.published.replace(tzinfo=None) > last_submission_date:
                    last_submission_date = result.published.replace(tzinfo=None)

                # Construct the message.
                message = (
                    f"📄 **New paper by {target_authors_str}:**\n"
                    f"**Title:** {title}\n"
                    f"**Authors:** {', '.join(result_authors)}\n"
                    f"**Submitted:** {published_str}\n"
                    f"**Abstract:** {summary}\n"
                    f"{f'**Journal Reference:** {journal_ref}\n' if journal_ref else ''}"
                    f"🔗 <{link}>"
                )

                logging.info(f"Sending message for paper: '{title}' with target authors: {target_authors_str}.")
                # print length of message to console
                # logging.info(f"Message length: {len(message)}")
                await channel.send(message)
                papers_posted += 1

            logging.info(f"Posted {papers_posted} new papers.")

            # Save the last submission date to the file, if any papers were posted.
            if papers_posted > 0:
                logging.info(f"Last submission date found: {last_submission_date}")
                if NO_SAVE:
                    logging.info("Skipping save of last submission date due to --nosave flag.")
                else:
                    save_last_submission_time(last_submission_date + timedelta(seconds=1))
                    logging.info("Saved last submission date.")

        except Exception as e:
            logging.exception(f"Error during combined query processing: {e}")

# ----------------------------------------------------------------------------
# Main Async Function to Start the Bot
# ----------------------------------------------------------------------------
async def main():
    intents = discord.Intents.default()
    bot = ArxivBot(intents=intents)
    try:
        await bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        logging.info("Shutdown requested...")
    finally:
        if not bot.is_closed():
            logging.info("Closing bot connection...")
            await bot.close()
        # Clean up any pending tasks
        pending = asyncio.all_tasks(asyncio.get_event_loop())
        for task in pending:
            if task is not asyncio.current_task():
                try:
                    logging.info("Cancelling pending task...")
                    task.cancel()
                    await task
                except asyncio.CancelledError:
                    pass
        # For Discord.py 2.0+ ensure the aiohttp session is closed.
        if hasattr(bot, 'http') and hasattr(bot.http, '_session'):
            if not bot.http._session.closed:
                logging.info("Closing aiohttp session...")
                await bot.http._session.close()

# ----------------------------------------------------------------------------
# Entry Point and Command Line Argument Parsing
# ----------------------------------------------------------------------------
NO_SAVE = "--nosave" in sys.argv
LAST_DATE_OVERRIDE = None
for arg in sys.argv:
    if arg.startswith("--lastdate="):
        try:
            date_str = arg.split("=", 1)[1]
            LAST_DATE_OVERRIDE = datetime.fromisoformat(date_str)
            logging.info(f"Overriding last submission date with: {date_str}")
        except Exception as e:
            logging.error(f"Invalid date format for --lastdate argument. Expected ISO format. Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
