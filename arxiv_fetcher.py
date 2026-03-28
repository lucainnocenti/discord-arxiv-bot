# arxiv_fetcher.py
"""
Handles fetching and normalizing paper data from the arXiv service,
supporting both the official API and the RSS feed.
"""

import asyncio
import arxiv # Library for interacting with the arXiv API
import feedparser # Library for parsing RSS/Atom feeds
import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime
from difflib import SequenceMatcher
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
    id: str              # Canonical arXiv identifier (for example, '2503.16215')
    title: str           # Paper title
    authors: List[str]   # List of author names
    published: datetime  # Published/Announced datetime (timezone-aware)
    summary: str         # Paper abstract/summary
    link: str            # Link to the abstract page (same as id)
    pdf_link: str        # Direct link to the PDF
    doi: Optional[str]   # DOI, if available
    journal_ref: Optional[str] # Journal reference, if available (e.g., "Phys. Rev. Lett. ...")
    announce_type: Optional[str] # Type of announcement from RSS (e.g., 'new', 'replace'), or 'api_new' for API results


ARXIV_ID_PATTERN = re.compile(r'(\d{4}\.\d{4,5})(v\d+)?')
TITLE_NORMALIZATION_PATTERN = re.compile(r'[^a-z0-9]+')
CROSSREF_API_URL = "https://api.crossref.org/works"


def canonicalize_paper_id(raw_id: str) -> str:
    """Converts arXiv URLs and OAI identifiers to a stable arXiv ID without the version suffix."""
    normalized = raw_id.strip()
    match = ARXIV_ID_PATTERN.search(normalized)
    if match:
        return match.group(1)

    if '/abs/' in normalized:
        normalized = normalized.split('/abs/', 1)[-1]
    elif normalized.lower().startswith('oai:arxiv.org:'):
        normalized = normalized.split(':', 2)[-1]

    return normalized.removeprefix('abs/').split('v', 1)[0]

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

    async def fetch_publication_updates(self, tracked_paper_ids: List[str]) -> List[Paper]:
        """
        Refreshes tracked arXiv papers and returns the ones that now look published.

        arXiv metadata is treated as the primary source of truth because a DOI or
        journal reference appearing there is the clearest signal that the authors
        have linked the preprint to its journal publication. Crossref is used as a
        conservative fallback when arXiv metadata has not been updated yet.
        """
        if not tracked_paper_ids:
            return []

        tracked_papers = await self._fetch_papers_by_ids(tracked_paper_ids)
        publication_updates: List[Paper] = []
        loop = asyncio.get_event_loop()

        for paper in tracked_papers:
            # If arXiv itself already exposes publication metadata, prefer that
            # over any external inference.
            if paper.journal_ref or paper.doi:
                enriched = await self.enrich_publication_metadata(paper)
                publication_updates.append(enriched._replace(announce_type='published_metadata'))
                continue

            # Crossref is only consulted for papers that still look unpublished
            # on arXiv, which helps keep false positives low.
            crossref_match = await loop.run_in_executor(None, self._find_crossref_publication, paper)
            if crossref_match:
                publication_updates.append(crossref_match)

        return publication_updates

    async def enrich_publication_metadata(self, paper: Paper) -> Paper:
        """Normalizes journal metadata and upgrades sparse refs when Crossref can help."""
        # Normalize first so later checks do not have to care about blank strings
        # versus None when deciding whether metadata is actually present.
        normalized_paper = paper._replace(
            doi=self._clean_optional_text(paper.doi),
            journal_ref=self._clean_optional_text(paper.journal_ref),
        )
        if not normalized_paper.doi and not normalized_paper.journal_ref:
            return normalized_paper

        loop = asyncio.get_event_loop()

        if normalized_paper.doi:
            # A DOI is the strongest key Crossref offers, so try that before any
            # fuzzier title/author matching.
            crossref_item = await loop.run_in_executor(None, self._fetch_crossref_work_by_doi, normalized_paper.doi)
            if crossref_item:
                enriched = self._paper_from_crossref_item(
                    normalized_paper,
                    crossref_item,
                    announce_type='crossref_doi',
                )
                if enriched:
                    return enriched

        if self._journal_ref_needs_enrichment(normalized_paper.journal_ref):
            # Fall back to a conservative search only when the existing journal
            # reference looks too sparse to be useful in Discord messages.
            crossref_match = await loop.run_in_executor(None, self._find_crossref_publication, normalized_paper)
            if crossref_match:
                return crossref_match

        return normalized_paper

    async def _fetch_from_api(self, last_submission_date: datetime) -> List[Paper]:
        """
        Fetches papers using the arXiv API, filtering by category, target authors, and submission date.

        Args:
            last_submission_date: Timestamp to filter papers (only newer papers).

        Returns:
            A list of normalized Paper objects fetched from the API. Returns empty list on API error.
        """
        # Format the date for the arXiv API query (YYYYMMDDHHMMSS format, assumed UTC)
        # The `last_submission_date` passed in should ideally be timezone-naive or UTC
        # for consistent comparison with arXiv's submittedDate field.
        date_query_str = last_submission_date.strftime("%Y%m%d%H%M%S")

        all_authors = self.settings.target_authors
        filtered_authors: List[str] = []
        seen_filtered = set()
        for author in all_authors:
            # The API query is more reliable with full names than with bare
            # initials, so skip low-signal variants when a better form exists.
            parts = author.split()
            if len(parts) < 2:
                continue
            first_part = parts[0]
            if len(first_part) <= 1 or first_part.endswith('.'):
                continue
            if author in seen_filtered:
                continue
            seen_filtered.add(author)
            filtered_authors.append(author)

        authors = filtered_authors if filtered_authors else all_authors
        if len(authors) != len(all_authors):
            self.logger.info(
                f"Using {len(authors)} canonical author names for API query "
                f"(filtered from {len(all_authors)} configured names)."
            )

        if not authors:
            author_chunks: List[List[str]] = [[]]
        else:
            max_authors_per_query = 20
            # Split large author lists so the final query string stays within
            # the size arXiv accepts and failures affect only one chunk.
            author_chunks = [
                authors[i:i + max_authors_per_query]
                for i in range(0, len(authors), max_authors_per_query)
            ]

        loop = asyncio.get_event_loop()
        unique_results: List[arxiv.Result] = []
        seen_entry_ids = set()

        for index, author_chunk in enumerate(author_chunks, start=1):
            if author_chunk:
                authors_query = ' OR '.join(f'au:"{author}"' for author in author_chunk)
                query = (
                    f'cat:{self.settings.category} AND '
                    f'({authors_query}) AND '
                    f'submittedDate:[{date_query_str} TO 99991231235959]'
                )
            else:
                query = (
                    f'cat:{self.settings.category} AND '
                    f'submittedDate:[{date_query_str} TO 99991231235959]'
                )

            self.logger.info(
                f"Constructed API query chunk {index}/{len(author_chunks)} with "
                f"{len(author_chunk)} authors."
            )

            search = arxiv.Search(
                query=query,
                max_results=self.settings.max_results,
                sort_by=arxiv.SortCriterion.SubmittedDate,
                sort_order=arxiv.SortOrder.Ascending,
            )

            chunk_results: List[arxiv.Result] = []
            for attempt in range(1, 3):
                try:
                    results_iterator = self.arxiv_client.results(search)
                    chunk_results = await loop.run_in_executor(None, list, results_iterator)
                    self.logger.info(
                        f"arXiv API chunk {index}/{len(author_chunks)} returned {len(chunk_results)} results."
                    )
                    break
                except Exception as e:
                    is_rate_limited = "HTTP 429" in str(e)
                    is_last_attempt = attempt == 2

                    if is_rate_limited and not is_last_attempt:
                        self.logger.warning(
                            f"Rate-limited on chunk {index}/{len(author_chunks)} (attempt {attempt}). "
                            f"Waiting before retry."
                        )
                        await asyncio.sleep(10)
                        continue

                    self.logger.error(
                        f"Error during arXiv API search for chunk {index}/{len(author_chunks)}: {e}",
                        exc_info=True,
                    )
                    break

            for result in chunk_results:
                # Multiple author chunks can surface the same paper, so dedupe
                # before normalization to keep downstream posting stable.
                if result.entry_id in seen_entry_ids:
                    continue
                seen_entry_ids.add(result.entry_id)
                unique_results.append(result)

            if index < len(author_chunks):
                await asyncio.sleep(3)

        unique_results.sort(key=lambda r: r.published)

        # Normalize each result from the API into our standard Paper format
        normalized_papers = [self._normalize_api_result(result) for result in unique_results]
        return normalized_papers

    async def _fetch_papers_by_ids(self, paper_ids: List[str]) -> List[Paper]:
        """Fetches specific arXiv papers by ID to refresh their metadata."""
        normalized_ids: List[str] = []
        seen_ids = set()
        for paper_id in paper_ids:
            canonical_id = canonicalize_paper_id(paper_id)
            if canonical_id in seen_ids:
                continue
            seen_ids.add(canonical_id)
            normalized_ids.append(canonical_id)

        if not normalized_ids:
            return []

        loop = asyncio.get_event_loop()
        # Rebuild results in the original order later so the caller gets stable,
        # predictable processing independent of API response order.
        papers_by_id: Dict[str, Paper] = {}
        chunk_size = 50
        id_chunks = [
            normalized_ids[i:i + chunk_size]
            for i in range(0, len(normalized_ids), chunk_size)
        ]

        for index, id_chunk in enumerate(id_chunks, start=1):
            self.logger.info(
                f"Refreshing publication metadata for chunk {index}/{len(id_chunks)} "
                f"({len(id_chunk)} tracked papers)."
            )
            search = arxiv.Search(id_list=id_chunk, max_results=len(id_chunk))

            try:
                results_iterator = self.arxiv_client.results(search)
                chunk_results = await loop.run_in_executor(None, list, results_iterator)
            except Exception as e:
                self.logger.error(
                    f"Error refreshing tracked paper metadata for chunk {index}/{len(id_chunks)}: {e}",
                    exc_info=True,
                )
                continue

            for result in chunk_results:
                paper = self._normalize_api_result(result)
                papers_by_id[paper.id] = paper

            if index < len(id_chunks):
                await asyncio.sleep(3)

        return [papers_by_id[paper_id] for paper_id in normalized_ids if paper_id in papers_by_id]

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
        for entry in feed.entries:
            try:
                # Attempt to normalize the raw RSS entry into our Paper structure
                paper = self._normalize_rss_entry(entry)

                # RSS filtering happens client-side because the feed itself only
                # filters by category, not by author.
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
            id=canonicalize_paper_id(entry_id_url),
            title=result.title.strip(), # Clean title whitespace
            authors=[author.name for author in result.authors], # Extract author names
            published=published_dt, # Use the timezone-aware datetime
            summary=summary_cleaned,
            link=entry_id_url, # Link is the same as the ID (abstract URL)
            pdf_link=pdf_link,
            doi=self._clean_optional_text(result.doi), # DOI may be present for journal-published works
            journal_ref=self._clean_optional_text(result.journal_ref), # May be None if not available
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
        doi = getattr(entry, 'arxiv_doi', None) # Standard key in arXiv RSS for DOI, when available
        announce_type = getattr(entry, 'arxiv_announce_type', 'rss_unknown') # Key for 'new', 'replace', etc.

        # Ensure optional fields are strings or None (handle potential non-string types gracefully)
        if journal_ref is not None and not isinstance(journal_ref, str): journal_ref = str(journal_ref)
        if doi is not None and not isinstance(doi, str): doi = str(doi)
        if announce_type is not None and not isinstance(announce_type, str): announce_type = str(announce_type)

        # --- Final Assembly into Paper Object ---
        # All required fields have been validated or have fallbacks by this point.
        return Paper(
            id=canonicalize_paper_id(entry_id_url),
            title=title.strip(),    # Validated string, clean whitespace
            authors=authors,        # List[str] (might be empty if parsing failed)
            published=published_dt, # Validated timezone-aware datetime
            summary=summary,        # Cleaned string
            link=link,              # Validated string
            pdf_link=pdf_link,      # Constructed string
            doi=self._clean_optional_text(doi),                # Optional[str]
            journal_ref=self._clean_optional_text(journal_ref), # Optional[str]
            announce_type=announce_type # Optional[str]
        )

    def _find_crossref_publication(self, paper: Paper) -> Optional[Paper]:
        """Queries Crossref for a likely journal-article match when arXiv metadata is incomplete."""
        if not paper.title or not paper.authors:
            return None

        # Keep the query narrow: same title, same first author, journal articles
        # only, and just the fields needed for the publication-update decision.
        params = {
            "rows": "5",
            "query.title": paper.title,
            "query.author": paper.authors[0],
            "filter": "type:journal-article",
            "select": "DOI,title,author,container-title,type,volume,issue,page,article-number,published-print,published-online,issued",
        }
        if self.settings.crossref_mailto:
            params["mailto"] = self.settings.crossref_mailto

        request = urllib.request.Request(
            f"{CROSSREF_API_URL}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": self._crossref_user_agent()},
        )

        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except Exception as e:
            self.logger.warning(f"Crossref lookup failed for {paper.id}: {e}")
            return None

        items = payload.get("message", {}).get("items", [])
        if not isinstance(items, list):
            return None

        for item in items:
            # Stop at the first high-confidence match; this path is meant to be
            # conservative, not exhaustive.
            candidate = self._crossref_item_to_publication_update(paper, item)
            if candidate:
                self.logger.info(f"Crossref matched tracked paper {paper.id} to DOI {candidate.doi}.")
                return candidate

        return None

    def _fetch_crossref_work_by_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        """Fetches a specific Crossref work by DOI."""
        request = urllib.request.Request(
            f"{CROSSREF_API_URL}/{urllib.parse.quote(doi)}",
            headers={"User-Agent": self._crossref_user_agent()},
        )

        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except Exception as e:
            self.logger.warning(f"Crossref DOI lookup failed for {doi}: {e}")
            return None

        item = payload.get("message")
        return item if isinstance(item, dict) else None

    def _crossref_item_to_publication_update(self, paper: Paper, item: Dict[str, Any]) -> Optional[Paper]:
        """Validates a Crossref record and converts it into a publication update."""
        if item.get("type") != "journal-article":
            return None

        doi = item.get("DOI")
        titles = item.get("title", [])
        authors = item.get("author", [])
        if not isinstance(doi, str) or not titles:
            return None

        crossref_title = titles[0] if isinstance(titles[0], str) else ""
        if not crossref_title or not self._is_probable_crossref_match(paper, crossref_title, authors):
            return None

        return self._paper_from_crossref_item(paper, item, announce_type='crossref_published')

    def _is_probable_crossref_match(
        self,
        paper: Paper,
        crossref_title: str,
        crossref_authors: Any,
    ) -> bool:
        """Uses a conservative title+author check to avoid false-positive DOI matches."""
        # Publication updates are much noisier than new-paper detection, so this
        # intentionally errs on the side of missing a match rather than claiming
        # the wrong DOI for a tracked preprint.
        paper_title = self._normalize_title_for_match(paper.title)
        candidate_title = self._normalize_title_for_match(crossref_title)
        if not paper_title or not candidate_title:
            return False

        title_similarity = SequenceMatcher(None, paper_title, candidate_title).ratio()
        if title_similarity < 0.93 and paper_title != candidate_title:
            return False

        first_author_family = self._extract_family_name(paper.authors[0])
        if not first_author_family:
            return False

        candidate_families = set()
        if isinstance(crossref_authors, list):
            for author in crossref_authors:
                if not isinstance(author, dict):
                    continue
                family_name = author.get("family")
                full_name = author.get("name")
                if isinstance(family_name, str):
                    candidate_families.add(self._extract_family_name(family_name))
                elif isinstance(full_name, str):
                    candidate_families.add(self._extract_family_name(full_name))

        return first_author_family in candidate_families

    def _crossref_user_agent(self) -> str:
        """Builds a polite Crossref User-Agent string."""
        if self.settings.crossref_mailto:
            return f"arxiv-discord-bot/1.0 (mailto:{self.settings.crossref_mailto})"
        return "arxiv-discord-bot/1.0"

    def _paper_from_crossref_item(
        self,
        paper: Paper,
        item: Dict[str, Any],
        *,
        announce_type: str,
    ) -> Optional[Paper]:
        """Merges the best DOI and journal reference available from a Crossref item."""
        doi = self._clean_optional_text(item.get("DOI")) or paper.doi
        journal_ref = self._choose_better_journal_ref(
            paper.journal_ref,
            self._format_crossref_journal_ref(item),
        )

        if not doi and not journal_ref:
            return None

        return paper._replace(
            doi=doi,
            journal_ref=journal_ref,
            announce_type=announce_type,
        )

    def _format_crossref_journal_ref(self, item: Dict[str, Any]) -> Optional[str]:
        """Builds a consistent citation string from a Crossref work when enough fields exist."""
        container_titles = item.get("container-title", [])
        journal = None
        if isinstance(container_titles, list) and container_titles:
            first_container_title = container_titles[0]
            if isinstance(first_container_title, str):
                journal = self._clean_optional_text(first_container_title)

        if not journal:
            return None

        volume = self._clean_optional_text(item.get("volume"))
        issue = self._clean_optional_text(item.get("issue"))
        pages = self._clean_optional_text(item.get("page")) or self._clean_optional_text(item.get("article-number"))
        year = self._extract_crossref_year(item)

        citation = journal
        if volume:
            citation += f" {volume}"
            if issue:
                citation += f".{issue}"
        if year:
            citation += f" ({year})"
        if pages:
            citation += f": {pages}"

        return citation

    def _extract_crossref_year(self, item: Dict[str, Any]) -> Optional[str]:
        """Returns the most useful year present in a Crossref record."""
        for field in ("published-print", "published-online", "issued"):
            date_part = item.get(field)
            if not isinstance(date_part, dict):
                continue
            parts = date_part.get("date-parts")
            if not isinstance(parts, list) or not parts:
                continue
            first = parts[0]
            if not isinstance(first, list) or not first:
                continue
            year = first[0]
            if isinstance(year, int):
                return str(year)
            if isinstance(year, str):
                cleaned = year.strip()
                if cleaned:
                    return cleaned
        return None

    def _choose_better_journal_ref(self, current: Optional[str], candidate: Optional[str]) -> Optional[str]:
        """Prefers the more informative journal reference."""
        current_clean = self._clean_optional_text(current)
        candidate_clean = self._clean_optional_text(candidate)
        if not candidate_clean:
            return current_clean
        if not current_clean:
            return candidate_clean
        if self._journal_ref_score(candidate_clean) > self._journal_ref_score(current_clean):
            return candidate_clean
        return current_clean

    def _journal_ref_needs_enrichment(self, journal_ref: Optional[str]) -> bool:
        """Returns True when the current journal reference looks sparse."""
        cleaned = self._clean_optional_text(journal_ref)
        if not cleaned:
            return True
        return self._journal_ref_score(cleaned) < 2

    def _journal_ref_score(self, journal_ref: str) -> int:
        """Scores how citation-like a journal reference is."""
        score = 0
        if re.search(r'\b\d{4}\b', journal_ref):
            score += 1
        if re.search(r'\b\d+(?:\.\d+)?\b', journal_ref):
            score += 1
        if ":" in journal_ref or re.search(r'\b[A-Z]?\d{4,}\b', journal_ref):
            score += 1
        return score

    def _clean_optional_text(self, value: Any) -> Optional[str]:
        """Strips text fields and normalizes blank strings to None."""
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    def _normalize_title_for_match(self, title: str) -> str:
        """Lowercases and strips punctuation so near-identical titles compare reliably."""
        return TITLE_NORMALIZATION_PATTERN.sub('', title.casefold())

    def _extract_family_name(self, author_name: str) -> str:
        """Extracts a loose last-name token for conservative author matching."""
        tokens = TITLE_NORMALIZATION_PATTERN.sub(' ', author_name.casefold()).split()
        return tokens[-1] if tokens else ""
