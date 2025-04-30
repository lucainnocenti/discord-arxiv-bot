# arxiv_fetcher.py
"""
Handles fetching and normalizing paper data from the arXiv service,
supporting both the official API and the RSS feed.
"""

import asyncio
import arxiv # Library for interacting with the arXiv API
import feedparser # Library for parsing RSS/Atom feeds
import logging
from datetime import datetime
from zoneinfo import ZoneInfo # For timezone handling (especially UTC and ET)
import time # Needed for type hinting time.struct_time from feedparser
from typing import List, Dict, Any, NamedTuple, Optional, cast, Tuple # For type hinting

# Import settings and utilities from other modules in the project
from settings import AppSettings, API_SOURCE, RSS_SOURCE
from utils import decode_author_name

# Define a standard structure for paper data returned by the fetcher.
# Using NamedTuple provides immutability and dot-notation access.
class Paper(NamedTuple):
    """Represents a normalized arXiv paper with key details."""
    id: str              # Unique identifier (usually the arXiv URL: http://arxiv.org/abs/...)
    title: str           # Paper title
    authors: List[str]   # List of author names
    published: datetime  # Published/Announced datetime (timezone-aware)
    summary: str         # Paper abstract/summary
    link: str            # Link to the abstract page (same as id)
    pdf_link: str        # Direct link to the PDF
    journal_ref: Optional[str] # Journal reference, if available (e.g., "Phys. Rev. Lett. ...")
    announce_type: Optional[str] # Type of announcement from RSS (e.g., 'new', 'replace'), or 'api_new' for API results

class ArxivFetcher:
    """
    Fetches and normalizes paper information from arXiv using either the API or RSS feed.
    """
    def __init__(self, settings: AppSettings):
        """
        Initializes the fetcher with application settings and the arXiv API client.

        Args:
            settings: An AppSettings object containing configuration like target authors, category, etc.
        """
        self.settings = settings
        # Initialize the arXiv client once and reuse it.
        # page_size, delay_seconds, num_retries help manage API rate limits and transient errors.
        self.arxiv_client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=3)
        self.logger = logging.getLogger(self.__class__.__name__) # Get a logger specific to this class

    async def fetch_latest_papers(self, last_submission_date_api: datetime) -> List[Paper]:
        """
        Fetches papers from the configured source (API or RSS) based on settings.

        This is the main entry point for fetching data. It routes the request
        to the appropriate private method (_fetch_from_api or _fetch_from_rss).

        Args:
            last_submission_date_api: The timestamp used for filtering API results.
                                      Only papers submitted after this time are fetched via API.
                                      This argument is ignored if the source is RSS.

        Returns:
            A list of Paper objects matching the criteria. Returns an empty list on error.

        Raises:
            ValueError: If an invalid source is configured in settings.
        """
        loop = asyncio.get_event_loop()
        papers: List[Paper] = []

        # Route fetching based on the configured source
        if self.settings.source == API_SOURCE:
            self.logger.info("Fetching papers using the arXiv API.")
            # API fetch requires the last submission date for filtering
            papers = await self._fetch_from_api(last_submission_date_api)

        elif self.settings.source == RSS_SOURCE:
            self.logger.info("Fetching papers using the arXiv RSS feed.")
            # feedparser is blocking, so run it in an executor thread to avoid blocking the event loop
            papers = await loop.run_in_executor(None, self._fetch_from_rss)

        else:
            # This should ideally be caught by settings validation, but serves as a safeguard.
            self.logger.error(f"Invalid source configured: {self.settings.source}")
            raise ValueError(f"Invalid source: {self.settings.source}")

        self.logger.info(f"Found {len(papers)} papers matching criteria using source '{self.settings.source}'.")
        return papers

    async def _fetch_from_api(self, last_submission_date: datetime) -> List[Paper]:
        """
        Fetches papers using the arXiv API, filtering by category, target authors, and submission date.

        Args:
            last_submission_date: Timestamp to filter papers (only newer papers).

        Returns:
            A list of normalized Paper objects fetched from the API. Returns empty list on API error.
        """
        # Construct the author part of the query (match any of the target authors)
        authors_query = ' OR '.join(f'au:"{author}"' for author in self.settings.target_authors)

        # Format the date for the arXiv API query (YYYYMMDDHHMMSS format, assumed UTC)
        # The `last_submission_date` passed in should ideally be timezone-naive or UTC
        # for consistent comparison with arXiv's submittedDate field.
        date_query_str = last_submission_date.strftime("%Y%m%d%H%M%S")

        # Combine category, authors, and date into the final query string
        query = (
            f'cat:{self.settings.category} AND '
            f'({authors_query}) AND '
            f'submittedDate:[{date_query_str} TO 99999999]' # Papers submitted from last_date onwards
        )
        self.logger.info(f"Constructed API query: {query}")

        # Create the search object with sorting preferences
        search = arxiv.Search(
            query=query,
            max_results=self.settings.max_results,
            sort_by=arxiv.SortCriterion.SubmittedDate, # Sort by submission date
            sort_order=arxiv.SortOrder.Ascending,     # Get oldest matching first (usually desired)
        )

        try:
            # Get the current event loop
            loop = asyncio.get_event_loop()
            # The arxiv library's search is blocking, run it in an executor thread
            results_iterator = self.arxiv_client.results(search)
            results = await loop.run_in_executor(None, list, results_iterator) # Convert iterator to list
            self.logger.info(f"arXiv API returned {len(results)} results.")
        except Exception as e:
            # Log errors during the API call
            self.logger.error(f"Error during arXiv API search: {e}", exc_info=True)
            return [] # Return an empty list if the API call fails

        # Normalize each result from the API into our standard Paper format
        normalized_papers = [self._normalize_api_result(result) for result in results]
        return normalized_papers

    def _fetch_from_rss(self) -> List[Paper]:
        """
        Fetches papers from the arXiv RSS feed for a given category.
        Note: RSS feed filtering happens *after* fetching, based on authors.
              RSS does not support server-side date filtering like the API.

        Returns:
            A list of normalized Paper objects matching the target authors. Returns empty list on error.
        """
        feed_url = f"http://rss.arxiv.org/rss/{self.settings.category}"
        self.logger.info(f"Fetching RSS feed: {feed_url}")

        try:
            # Parse the feed. feedparser handles redirects (like 301) automatically.
            feed = feedparser.parse(feed_url)

            # Check for parsing errors indicated by feedparser (non-fatal usually)
            if feed.bozo:
                 # Log bozo errors but don't necessarily stop unless feed.entries is missing
                 self.logger.warning(f"Feedparser signaled potential issues parsing RSS feed (bozo): {getattr(feed, 'bozo_exception', 'Unknown reason')}")

            # Check the *final* HTTP status code *after* redirects.
            # Fail only on client (4xx) or server (5xx) errors. Allow 2xx (Success) and 3xx (Redirects).
            status = getattr(feed, 'status', None)
            if isinstance(status, list) and status:
                status = status[0]
            if isinstance(status, int) and status >= 400:
                 self.logger.error(f"Failed to fetch RSS feed content, final HTTP status code: {status}")
                 # Optionally log feed headers or content for debugging
                 # self.logger.debug(f"Feed Headers: {getattr(feed, 'headers', {})}")
                 return [] # Cannot proceed if the final fetch resulted in an error

            # Additional check: Ensure entries exist, even if status is okay/redirect
            if not hasattr(feed, 'entries') or not isinstance(feed.entries, list):
                 self.logger.error(f"RSS feed fetched (status: {getattr(feed, 'status', 'N/A')}) but no 'entries' list found or it's not a list. Feed structure might be invalid.")
                 return []

            self.logger.info(f"RSS feed fetched successfully (final status: {getattr(feed, 'status', 'N/A')}), found {len(feed.entries)} entries.")

        except Exception as e:
            # Catch any other exceptions during feed fetching/parsing
            self.logger.error(f"Exception occurred during RSS feed fetching/parsing for {feed_url}: {e}", exc_info=True)
            return [] # Return empty list on error

        papers: List[Paper] = []
        # Process up to max_results entries from the feed
        for entry in feed.entries[:self.settings.max_results]:
            try:
                # Attempt to normalize the raw RSS entry into our Paper structure
                paper = self._normalize_rss_entry(entry)

                # If normalization was successful, check if any author matches our target list
                if paper and self._is_author_match(paper.authors):
                    papers.append(paper) # Add the paper if it's valid and matches an author

            except Exception as e:
                 # Log errors during normalization of a specific entry but continue with others
                 entry_id_str = getattr(entry, 'id', 'N/A') # Try to get ID for logging
                 self.logger.warning(f"Skipping RSS entry ID '{entry_id_str}' due to error during normalization: {e}", exc_info=True)
                 continue # Move to the next entry

        return papers

    def _is_author_match(self, paper_authors: List[str]) -> bool:
        """
        Checks if any author in the paper's author list matches any of the target authors
        defined in the settings (case-insensitive).

        Args:
            paper_authors: A list of author names from a single paper.

        Returns:
            True if there is at least one match, False otherwise.
        """
        # Convert target authors to lowercase set for efficient lookup
        target_authors_lower = {ta.lower() for ta in self.settings.target_authors}
        # Convert paper authors to lowercase set
        paper_authors_lower = {pa.lower() for pa in paper_authors}
        # Check if the intersection of the two sets is non-empty
        return not target_authors_lower.isdisjoint(paper_authors_lower)

    def _normalize_api_result(self, result: arxiv.Result) -> Paper:
        """
        Converts a single result object from the arxiv API library into a standardized Paper NamedTuple.

        Args:
            result: An arxiv.Result object.

        Returns:
            A Paper object containing the normalized data.
        """
        # Extract the short arXiv ID (e.g., '2307.12345')
        paper_id_num = result.get_short_id()
        # Construct the standard PDF link
        pdf_link = f"http://arxiv.org/pdf/{paper_id_num}"
        # The entry_id from the API result is the canonical URL to the abstract page
        entry_id_url = result.entry_id

        # The 'published' field from the API result is typically timezone-aware (UTC)
        published_dt = result.published

        # Clean the summary: remove leading/trailing whitespace and replace newlines with spaces
        summary_cleaned = result.summary.strip().replace('\n', ' ')

        # Create and return the Paper object
        return Paper(
            id=entry_id_url, # Use the abstract URL as the unique ID
            title=result.title.strip(), # Clean title whitespace
            authors=[author.name for author in result.authors], # Extract author names
            published=published_dt, # Use the timezone-aware datetime
            summary=summary_cleaned,
            link=entry_id_url, # Link is the same as the ID (abstract URL)
            pdf_link=pdf_link,
            journal_ref=result.journal_ref, # May be None if not available
            announce_type='api_new' # Mark source as API; API doesn't distinguish announce types
        )

    def _normalize_rss_entry(self, entry: feedparser.FeedParserDict) -> Optional[Paper]:
        """
        Converts a single entry dictionary from a feedparser result into a standardized Paper NamedTuple.
        Includes robust checks for missing or malformed fields commonly found in feeds.

        Args:
            entry: A dictionary-like object representing an RSS entry.

        Returns:
            A Paper object if normalization is successful, otherwise None.
        """
        # --- Robust Validation and Access for Required Fields ---
        # Use getattr for safe access, checking type with isinstance. Log and return None if invalid.
        entry_id_url = getattr(entry, 'id', None)
        if not isinstance(entry_id_url, str):
            self.logger.warning(f"Skipping RSS entry: 'id' field missing or not a string. Entry data: {entry}")
            return None

        title = getattr(entry, 'title', None)
        if not isinstance(title, str):
            self.logger.warning(f"Skipping RSS entry ID '{entry_id_url}': 'title' field missing or not a string.")
            return None

        summary_raw = getattr(entry, 'summary', None)
        if not isinstance(summary_raw, str):
             self.logger.warning(f"Skipping RSS entry ID '{entry_id_url}': 'summary' field missing or not a string.")
             return None

        link = getattr(entry, 'link', None)
        if not isinstance(link, str):
             # Use entry_id_url as fallback if link is missing/invalid
             self.logger.debug(f"Using 'id' as fallback 'link' for RSS entry ID '{entry_id_url}'.")
             link = entry_id_url

        # --- Author Parsing (Handles common RSS format) ---
        authors: List[str] = []
        raw_authors_list = getattr(entry, 'authors', []) # Get the 'authors' attribute, default to empty list
        # Check if it's a list and has at least one element
        if isinstance(raw_authors_list, list) and raw_authors_list:
             # ArXiv RSS often puts all authors in a single string within the first dict: [{'name': 'Author A, Author B'}]
             author_dict = raw_authors_list[0]
             if isinstance(author_dict, dict):
                 # Safely get the 'name' key which should contain the comma-separated string
                 author_string = author_dict.get('name')
                 if isinstance(author_string, str):
                     # Split the string by comma, decode LaTeX, strip whitespace, and filter out empty strings
                     authors = [decode_author_name(name.strip()) for name in author_string.split(',') if name.strip()]

        # Log a warning if authors could not be parsed, but proceed (filtering might miss it later)
        if not authors:
            self.logger.warning(f"Could not parse authors for RSS entry ID: {entry_id_url}. Raw 'authors' field: {raw_authors_list}")
            # Depending on requirements, could `return None` here if authors are strictly needed.

        # --- ID and PDF Link Extraction ---
        try:
            # Extract the numerical ID part from the abstract URL (which is entry_id_url)
            paper_id_part = entry_id_url.split('/abs/')[-1]
            pdf_link = f"http://arxiv.org/pdf/{paper_id_part}"
        except Exception as e:
             self.logger.error(f"Failed to extract paper ID part from URL '{entry_id_url}' for entry: {e}", exc_info=True)
             return None # Cannot proceed without the ID part

        # --- Date Parsing (Handles feedparser's `published_parsed` and fallback) ---
        published_dt: Optional[datetime] = None
        try:
            # feedparser pre-parses dates into `published_parsed` (a time.struct_time)
            published_parsed_value = getattr(entry, 'published_parsed', None)
            if published_parsed_value:
                try:
                    # Cast to time.struct_time for type checker sanity.
                    # time.struct_time is like a tuple of 9 integers (year, mon, day, hour, min, sec, wday, yday, isdst)
                    parsed_tuple = cast(time.struct_time, published_parsed_value)
                    # Runtime check for safety: ensure it has at least 6 elements (Y, M, D, H, M, S)
                    if len(parsed_tuple) >= 6:
                         # Create datetime object using the first 6 elements. Assume UTC.
                         published_dt = datetime(*parsed_tuple[:6], tzinfo=ZoneInfo('UTC'))
                         self.logger.debug(f"Successfully parsed date from 'published_parsed' for {entry_id_url}")
                    else:
                         # Log if the tuple is malformed
                         self.logger.warning(f"Attribute 'published_parsed' for entry {entry_id_url} has too few elements: {parsed_tuple}. Attempting string fallback.")
                         published_parsed_value = None # Prevent reuse, force fallback
                except (TypeError, ValueError) as cast_err:
                     # Log if casting/using the tuple fails unexpectedly
                     self.logger.warning(f"Could not use 'published_parsed' for entry {entry_id_url} despite existing. Error: {cast_err}. Raw value: {published_parsed_value}. Attempting string fallback.")
                     published_parsed_value = None # Prevent reuse, force fallback

            # Fallback: If 'published_parsed' wasn't usable or present, try parsing the 'published' string
            if published_dt is None:
                 published_str = getattr(entry, 'published', None)
                 if isinstance(published_str, str):
                    self.logger.debug(f"Parsing published date string '{published_str}' for {entry_id_url}")
                    try:
                        # Common RSS/Atom date format with timezone offset (%z)
                        published_dt = datetime.strptime(published_str, '%a, %d %b %Y %H:%M:%S %z')
                    except ValueError as strp_err:
                         # Log error if string parsing fails with the expected format
                         self.logger.error(f"Error parsing published date string for RSS entry {entry_id_url} (value: '{published_str}', format: '%a, %d %b %Y %H:%M:%S %z'): {strp_err}")
                         return None # Cannot proceed without a valid date
                 else:
                    # Log if no date information is found at all
                    self.logger.warning(f"Could not find usable published date (neither parsed nor string) for RSS entry {entry_id_url}.")
                    return None # Cannot proceed without a date

        except Exception as e:
            # Catch any other unexpected errors during the date processing block
            pub_str = getattr(entry, 'published', 'N/A')
            pub_parsed = getattr(entry, 'published_parsed', 'N/A')
            self.logger.exception(f"Unexpected error during date processing for RSS entry {entry_id_url} (str='{pub_str}', parsed='{pub_parsed}'): {e}")
            return None

        # --- Summary Cleanup ---
        # Remove the "Abstract: " prefix often found in arXiv RSS summaries
        summary = summary_raw.split("Abstract: ", 1)[-1].strip().replace('\n', ' ')

        # --- Get Optional Fields Safely ---
        journal_ref = getattr(entry, 'arxiv_journal_reference', None) # Standard key in arXiv RSS for journal ref
        announce_type = getattr(entry, 'arxiv_announce_type', 'rss_unknown') # Key for 'new', 'replace', etc.

        # Ensure optional fields are strings or None (handle potential non-string types gracefully)
        if journal_ref is not None and not isinstance(journal_ref, str): journal_ref = str(journal_ref)
        if announce_type is not None and not isinstance(announce_type, str): announce_type = str(announce_type)

        # --- Final Assembly into Paper Object ---
        # All required fields have been validated or have fallbacks by this point.
        return Paper(
            id=entry_id_url,        # Validated string
            title=title.strip(),    # Validated string, clean whitespace
            authors=authors,        # List[str] (might be empty if parsing failed)
            published=published_dt, # Validated timezone-aware datetime
            summary=summary,        # Cleaned string
            link=link,              # Validated string
            pdf_link=pdf_link,      # Constructed string
            journal_ref=journal_ref, # Optional[str]
            announce_type=announce_type # Optional[str]
        )