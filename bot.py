import discord                           # Discord library to interact with the Discord API
import asyncio                           # For asynchronous event handling and scheduling tasks
import arxiv                             # For querying the arXiv API for research papers
import os                                # For file path and OS interactions
import sys                               # For system-specific parameters and functions
import logging                           # For logging messages to both console and file
import config                            # Module to import configuration data such as tokens and IDs
from datetime import datetime, timedelta # For date and time handling
from zoneinfo import ZoneInfo
import feedparser                        # For parsing RSS feeds
import typing
from typing import List, Dict, Any, Optional  # For type annotations
from pylatexenc.latex2text import LatexNodes2Text


# ----------------------------------------------------------------------------
def decode_author_name(name: str) -> str:
    """
    Converts LaTeX-style encoded strings (e.g. "Albarr\\'an") to proper Unicode.
    """
    return LatexNodes2Text().latex_to_text(name)


# ----------------------------------------------------------------------------
# File and Directory Setup
# ----------------------------------------------------------------------------
# Determine the directory where the current script is located.
script_dir: str = os.path.dirname(os.path.abspath(__file__))

# Build the absolute path for the log file.
log_path: str = os.path.join(script_dir, "bot.log")
# Define the file that stores the last time arXiv was checked.
LAST_SUBMISSION_FILE: str = os.path.join(script_dir, "last_submission_date.txt")
LAST_RSS_CHECK_FILE: str = os.path.join(script_dir, "last_rss_check.txt")

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
DISCORD_TOKEN: str = config.DISCORD_TOKEN   # Bot's Discord authentication token
CHANNEL_ID: int = config.CHANNEL_ID         # Channel ID where messages are sent
TEST_CHANNEL_ID: int = config.TEST_CHANNEL_ID         # Channel ID where messages are sent
TARGET_AUTHORS: List[str] = config.TARGET_AUTHORS  # List of target authors
AUTHOR_DISCORD_IDS: Dict[str, int] = config.AUTHOR_DISCORD_IDS  # Mapping from author names to their Discord user IDs

# Global set to track posted papers during runtime (using paper IDs)
posted_papers: set = set()

# ----------------------------------------------------------------------------
# Paper Fetching Functions
# ----------------------------------------------------------------------------
def get_latest_papers_from_rss(category: str = "quant-ph", max_results: int = 100) -> List[Dict[str, Any]]:
    """
    Fetches the latest papers from the arXiv RSS feed for a given category and filters
    the papers by target authors (ignoring the published date because the RSS feed returns
    the same date for all entries).
    
    Args:
        category: The arXiv category (default: "quant-ph").
        max_results: Maximum number of results to process (default: 100).
    
    Returns:
        A list of dictionaries containing normalized paper details that match the target authors.
    """
    feed_url: str = f"http://rss.arxiv.org/rss/{category}"
    feed = feedparser.parse(feed_url)
    papers: List[Dict[str, Any]] = []
    
    for entry in feed.entries[:max_results]:
        # Extract authors from the RSS entry; each author is a dictionary.

        # this bullshit here is to stop Pylance from complaining about the type of entry.authors
        authors_list = entry.get('authors', [])
        if not isinstance(authors_list, list) or len(authors_list) == 0:
            raise ValueError(f"Invalid authors list for entry {entry.id}")
        namevalue = authors_list[0].get('name', None)
        if not isinstance(namevalue, str):
            raise ValueError(f"Invalid author name for entry {entry.id}")

        authors: List[str] = [
            decode_author_name(author)
            for author in namevalue.split(', ')
        ]
        
        # Only include the paper if at least one author matches one of the TARGET_AUTHORS (case-insensitive).
        if not any(target.lower() == author.lower() for target in TARGET_AUTHORS for author in authors):
            continue
        
        # Build the PDF link using the paper ID extracted from the entry URL.
        paper_id: str = typing.cast(str, entry.id).split('/abs/')[-1]
        pdf_link: str = f"http://arxiv.org/pdf/{paper_id}"
        
        # Parse the published date from the RSS feed (should just be midnight eastern US time).
        try:
            published_dt: datetime = datetime.strptime(typing.cast(str, entry.published), '%a, %d %b %Y %H:%M:%S %z')
        except Exception as e:
            logging.error(f"Error parsing date for entry {entry.id}: {e}")
            continue  # Skip entries with parsing errors

        # the entry.summary returned by the RSS includes a bunch of info BEFORE the actual abstract (which starts after the string "Abstract: "). Remove it.
        summary = typing.cast(str, entry.summary).split("Abstract: ")[1]

        # Create a normalized paper dictionary.
        paper: Dict[str, Any] = {
            'id': entry.id,
            'title': entry.title,
            'authors': authors,
            'published': published_dt,
            'summary': summary,
            'link': entry.link,
            'pdf_link': pdf_link,
            'journal_ref': entry.get('arxiv_journal_reference', None),
            'announce_type': entry.arxiv_announce_type
        }

        papers.append(paper)
    
    return papers

async def get_latest_papers_from_api(last_submission_date: datetime, category: str = "quant-ph", max_results: int = 50) -> List[Dict[str, Any]]:
    """
    Fetches the latest papers using the arXiv API. The query filters by target authors and submission date.
    
    Args:
        last_submission_date: The datetime to filter papers (only newer than this date).
        category: The arXiv category (default: "quant-ph").
        max_results: Maximum number of results to fetch.
    
    Returns:
        A list of normalized paper dictionaries.
    """
    # Construct a query string that combines the category, target authors, and submission date filtering.
    authors_query: str = ' OR '.join(f'au:"{author}"' for author in TARGET_AUTHORS)
    query: str = f'cat:{category} AND ({authors_query})'
    query += f' AND submittedDate:[{last_submission_date.strftime("%Y%m%d%H%M%S")} TO 99999999]'
    logging.info(f"Constructed combined query: {query}")

    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Ascending,
    )
    
    # Use an executor to run the blocking API call without freezing the event loop.
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, lambda: list(arxiv.Client().results(search)))
    
    papers: List[Dict[str, Any]] = []
    for result in results:
        # Build the PDF link from the result entry ID.

        paper_id: str = result.entry_id
        pdf_link: str = f"http://arxiv.org/pdf/{paper_id.split('/abs/')[-1]}"
        paper: Dict[str, Any] = {
            'id': result.entry_id,
            'title': result.title,
            'authors': [author.name for author in result.authors],
            'published': result.published,
            'summary': result.summary,
            'link': result.entry_id,  # The entry ID typically serves as the paper URL.
            'pdf_link': pdf_link,
            'journal_ref': result.journal_ref,
            'announce_type': 'new'  # when using the API the announce_type is moot; you can only tell if it was EVER updated or not
        }
        papers.append(paper)
    return papers


async def fetch_latest_papers(
    source: str,
    category: str = "quant-ph",
    max_results: int = 50
) -> List[Dict[str, Any]]:
    """
    Fetches and normalizes papers from either the arXiv API or RSS feed based on the selected source.
    
    For the RSS source, filtering is handled within get_latest_papers_from_rss (by target authors only).
    
    Args:
        last_submission_date: Only used for the API source (papers submitted after this date).
        source: A string indicating the source to use ('api' or 'rss').
        category: The arXiv category to query.
        max_results: Maximum number of results to fetch.
    
    Returns:
        A list of normalized paper dictionaries.
    """
    loop = asyncio.get_event_loop()
    papers: List[Dict[str, Any]] = []
    
    if source == "api":
        logging.info("Fetching papers using the arXiv API.")
        if LAST_DATE_OVERRIDE is not None:
            last_submission_date: datetime = LAST_DATE_OVERRIDE
            logging.info("Using override date {LAST_DATE_OVERRIDE}.")
        else:
            last_submission_date: datetime = get_last_submission_date_from_file()
            logging.info(f"Using date found in file: {last_submission_date}")
        papers = await get_latest_papers_from_api(last_submission_date, category, max_results)
    elif source == "rss":
        logging.info("Fetching papers using the arXiv RSS feed.")
        already_checked_rss = already_checked_rss_today()
        if already_checked_rss and not FORCE_RSS_CHECK:
            logging.info("RSS feed was already checked today. Skipping.")
            return []
        if already_checked_rss and FORCE_RSS_CHECK:
            logging.info("Forcing RSS check even though it was already checked today.")

        # Run the RSS fetching in an executor to avoid blocking.
        papers = await loop.run_in_executor(None, lambda: get_latest_papers_from_rss(category, max_results))
    else:
        raise ValueError("Invalid source specified. Valid options are 'api' and 'rss'.")
    
    logging.info(f"Found {len(papers)} papers using source '{source}'.")
    return papers



def get_last_submission_date_from_file() -> datetime:
    """
    Reads the last checked date from a file.
    If the file doesn't exist or an error occurs, it's created with a default date (yesterday).
    
    Returns:
        The datetime representing the last time arXiv was checked.
    """
    
    if os.path.exists(LAST_SUBMISSION_FILE):
        try:
            with open(LAST_SUBMISSION_FILE, 'r') as f:
                date_str: str = f.read().strip()
                return datetime.fromisoformat(date_str)
        except Exception as e:
            logging.error(f"Error reading last check date: {e}")
            return datetime.now() - timedelta(days=1)
    else:
        newdate: datetime = datetime.now() - timedelta(days=1)
        with open(LAST_SUBMISSION_FILE, 'w') as f:
            f.write(newdate.isoformat())
        return newdate

def save_last_submission_time_to_file(time: datetime) -> None:
    """
    Saves the provided datetime as the last check date into LAST_SUBMISSION_FILE.
    
    Args:
        time: A datetime object to be saved.
    """
    try:
        with open(LAST_SUBMISSION_FILE, 'w') as f:
            f.write(time.isoformat())
        logging.info(f"Saved date {time.isoformat()} to {LAST_SUBMISSION_FILE}")
    except Exception as e:
        logging.error(f"Error saving last check date: {e}")

def already_checked_rss_today() -> bool:
    """
    Checks if the RSS feed was already checked today by comparing the current date with the one in LAST_RSS_CHECK_FILE.
    If the file doesn't exist or an error occurs, it's created with the current date.
    
    Uses Eastern Time (ET) timezone for date comparison since arXiv RSS updates around midnight ET.
    """
    try:
        
        if os.path.exists(LAST_RSS_CHECK_FILE):
            # Use explicit Eastern Time zone
            current_date = datetime.now(ZoneInfo('America/New_York')).date()
            with open(LAST_RSS_CHECK_FILE, 'r') as f:
                date_str: str = f.read().strip()
            last_check_date = datetime.fromisoformat(date_str).date()
            return last_check_date == current_date
        else:
            # File doesn't exist, create it with current date
            logging.info(f"Creating new file {LAST_RSS_CHECK_FILE} with current date.")
            with open(LAST_RSS_CHECK_FILE, 'w') as f:
                f.write(datetime.now(ZoneInfo('America/New_York')).isoformat())
            return False
    
    except Exception as e:
        logging.error(f"Error checking last RSS check date: {e}")
        return False

def build_target_authors_string(result_authors: List[str], target_authors: List[str], author_discord_ids: Dict[str, int]) -> str:
    """
    Constructs a human-friendly string tagging target authors (using Discord IDs when available).
    
    Args:
        result_authors: List of authors from the paper.
        target_authors: List of target author names.
        author_discord_ids: Mapping from author names to their Discord user IDs.
    
    Returns:
        A string with tagged author names or "unknown" if none match.
    """
    # Identify target authors that appear in the result.
    target_in_result: List[str] = [
        name for name in target_authors
        if any(name.lower() == author.lower() for author in result_authors)
    ]
    if not target_in_result:
        return "unknown"

    # Reorder so that if the first author is a target, they appear first.
    first_author: str = result_authors[0]
    for target in target_in_result:
        if target.lower() in first_author.lower():
            target_in_result.remove(target)
            target_in_result.insert(0, target)
            break

    # Build the string by tagging authors when a Discord ID is available.
    tagged_authors: List[str] = [
        f"<@{author_discord_ids[author]}>" if author in author_discord_ids else author
        for author in target_in_result
    ]
    if len(tagged_authors) == 1:
        return tagged_authors[0]
    # Concatenate all names with commas, with an "and" before the last name.
    return ", ".join(tagged_authors[:-1]) + " and " + tagged_authors[-1]

# ----------------------------------------------------------------------------
# Discord Bot Class
# ----------------------------------------------------------------------------
class ArxivBot(discord.Client):
    async def on_ready(self) -> None:
        """
        Called when the bot successfully connects to Discord.
        It logs the identity, performs a one-time check for new arXiv papers, and then shuts down.
        """
        logging.info(f"Logged in as {self.user}")
        await self.check_arxiv()
        await self.close()  # Terminate after processing to prevent a long-running process

    async def check_arxiv(self) -> None:
        """
        Checks for new arXiv papers, constructs messages with paper details, and posts them to a Discord channel.
        """
        await self.wait_until_ready()
        if USETESTCHANNEL:
            channel = self.get_channel(TEST_CHANNEL_ID)
        else:
            channel = self.get_channel(CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            logging.error("ERROR: Could not get channel. Please check CHANNEL_ID!")
            return
        logging.info(f"Connecting to the Discord channel: {channel}")

        # Fetch papers from the selected source (API or RSS)
        papers: List[Dict[str, Any]] = await fetch_latest_papers(source=SOURCE, category="quant-ph", max_results=50)

        papers_posted: int = 0
        for paper in papers:
            paper_id: str = paper['id']
            if paper_id in posted_papers:
                logging.info(f"Already posted '{paper['title']}', skipping.")
                continue

            # Build a human-friendly string with target authors, using Discord tagging if available.
            target_authors_str: str = build_target_authors_string(paper['authors'], TARGET_AUTHORS, AUTHOR_DISCORD_IDS)
            # record the paper ID to avoid reposting it (I don't know why the fuck I do this I'm pretty sure it's pointless)
            posted_papers.add(paper_id)

            # Truncate the summary if it is too long (b/c fucking Discord doesn't allow messages longer than 2000 characters).
            summary: str = paper['summary']
            if len(summary) > 1400:
                summary = summary[:1400] + '...[continue]'

            published_str: str = (paper['published'] - timedelta(days=1)).strftime('%Y-%m-%d')
            
            logging.info(f"Found new paper: '{paper['title']}' by {target_authors_str}, submitted on {published_str}.")

            # Precompute journal reference line if available.
            journal_line: str = f"**Journal Reference:** {paper['journal_ref']}\n" if paper.get('journal_ref') else ""

            # if we're using the RSS and we find a paper that's an update, and that paper has a journal_ref, we change the announcement message accordingly
            if SOURCE == "rss" and paper['announce_type'] == 'replace' and paper['journal_ref']:
                message: str = (
                    f"📄 **Update to paper by {target_authors_str}**:\n"
                    f"The arXiv paper <{paper['link']}> was published! Cheers! 🥂🍾\n"
                    f"**New journal reference:** {paper['journal_ref']}"
                )
            # otherwise use the standard message format
            else:
                message: str = (
                    f"📄 **New paper by {target_authors_str}:**\n"
                    f"**Title:** {paper['title']}\n"
                    f"**Authors:** {', '.join(paper['authors'])}\n"
                    f"**Submitted:** {published_str}\n"
                    f"**Abstract:** {summary}\n"
                    f"{journal_line}"
                    f"🔗 <{paper['link']}>"
                )
                # if the message is too long, try to only print the first author and the rest as "et al."
                if len(message) > 2000:
                    first_author: str = paper['authors'][0]
                    message = (
                        f"📄 **New paper by {target_authors_str}:**\n"
                        f"**Title:** {paper['title']}\n"
                        f"**Authors:** {first_author} et al.\n"
                        f"**Submitted:** {published_str}\n"
                        f"**Abstract:** {summary}\n"
                        f"{journal_line}"
                        f"🔗 <{paper['link']}>"
                    )

                    # if the message is still too long, throw an error and skip posting it
                    if len(message) > 2000:
                        logging.error(f"Message too long for paper '{paper['title']}'. Skipping.")
                        continue
            
            if NO_SEND:
                logging.info(f"Skipping Discord message for paper: {paper['title']}")
            else:
                logging.info(f"Sending Discord message for paper.")
                await channel.send(message)
                papers_posted += 1

        logging.info(f"Posted {papers_posted} new papers.")

        # Save the new last submission date if papers were posted (unless --nosave flag is active).
        if papers_posted > 0 and SOURCE == "api":
            if NO_SAVE:
                logging.info("Skipping save of last submission date due to --nosave flag.")
            else:
                # extract last submission date among the found papers
                last_submission_date = max(paper['published'].replace(tzinfo=None) for paper in papers)
                logging.info(f"Last submission date found: {last_submission_date}")
                # Add a small delta to avoid reprocessing the same paper.
                save_last_submission_time_to_file(last_submission_date + timedelta(seconds=1))
                logging.info("Saved last submission date.")
        elif SOURCE == "rss":
            if NO_SAVE:
                logging.info("Skipping save of last RSS check date due to --nosave flag.")
            else:
                # Save the current date as the last RSS check date.
                with open(LAST_RSS_CHECK_FILE, 'w') as f:
                    f.write(datetime.now(ZoneInfo('America/New_York')).isoformat())
                logging.info("Saved last RSS check date.")

# ----------------------------------------------------------------------------
# Main Async Function to Start the Bot
# ----------------------------------------------------------------------------
async def main() -> None:
    """
    Main entry point: initializes the Discord bot and handles graceful shutdown.
    """
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


# ----------------------------------------------------------------------------
# Entry Point and Command Line Argument Parsing
# ----------------------------------------------------------------------------
NO_SAVE: bool = "--nosave" in sys.argv
NO_SEND: bool = "--nosend" in sys.argv
LAST_DATE_OVERRIDE: Optional[datetime] = None
SOURCE: str = "rss"  # Default source is the arXiv API
FORCE_RSS_CHECK: bool = "--forcerss" in sys.argv
USETESTCHANNEL: bool = "--testchannel" in sys.argv

# Parse command-line arguments for date override and source selection.
for arg in sys.argv:
    if arg.startswith("--lastdate="):
        try:
            date_str: str = arg.split("=", 1)[1]
            LAST_DATE_OVERRIDE = datetime.fromisoformat(date_str)
            logging.info(f"Overriding last submission date with: {date_str}")
        except Exception as e:
            logging.error(f"Invalid date format for --lastdate argument. Expected ISO format. Error: {e}")
    if arg.startswith("--source="):
        source_val: str = arg.split("=", 1)[1].lower()
        if source_val in {"api", "rss"}:
            SOURCE = source_val
            logging.info(f"Using source: {SOURCE}")
        else:
            logging.error("Invalid source specified. Valid options are 'api' and 'rss'. Using default 'api'.")

if SOURCE == "rss" and LAST_DATE_OVERRIDE is not None:
    raise ValueError("Custom lastdate is not allowed when source is 'rss' as they are not compatible.")

if SOURCE == "api" and FORCE_RSS_CHECK:
    raise ValueError("The --forcerss flag is not allowed with the 'api' source as it is only relevant for the RSS feed.")

if __name__ == "__main__":
    asyncio.run(main())
