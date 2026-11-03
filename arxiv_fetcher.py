# arxiv_fetcher.py
"""
Handles fetching and normalizing paper data from the arXiv API, plus
publication-metadata lookups on arXiv and Crossref.
"""

import asyncio
import arxiv # Library for interacting with the arXiv API
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo # For timezone handling (especially UTC and ET)
from typing import List, Dict, Any, NamedTuple, Optional, Set, Tuple # For type hinting

# Import settings and utilities from other modules in the project
from settings import AppSettings
from utils import decode_author_name, find_matching_target_authors

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
    announce_type: Optional[str] # Where the record came from, e.g. 'api_new' or 'crossref_doi'


class FetchPapersByIdResult(NamedTuple):
    """Summarizes a tracked-paper metadata refresh attempt."""
    papers: List[Paper]
    requested_ids: List[str]
    failed_ids: List[str]
    degraded: bool


class PublicationRefreshResult(NamedTuple):
    """Summarizes publication-update detection for one bot run."""
    updates: List[Paper]
    checked_ids: List[str]
    failed_ids: List[str]
    degraded: bool
    # Papers that got a Crossref lookup this run (their schedule restarts).
    crossref_checked_ids: List[str] = []


class TrackedPaperMetadata(NamedTuple):
    """Stored metadata retained locally so Crossref can run without a fresh arXiv hit."""
    title: Optional[str]
    authors: List[str]
    doi: Optional[str]
    journal_ref: Optional[str]


ARXIV_ID_PATTERN = re.compile(r'(\d{4}\.\d{4,5})(v\d+)?')
TITLE_NORMALIZATION_PATTERN = re.compile(r'[^a-z0-9]+')
CROSSREF_API_URL = "https://api.crossref.org/works"
# Crossref asks clients to stay well below its rate limit; one request per
# second keeps the bot comfortably polite.
CROSSREF_MIN_INTERVAL_SECONDS = 1.0
CROSSREF_MAX_RETRY_AFTER_SECONDS = 60
HTTP_STATUS_PATTERN = re.compile(r'HTTP (\d{3})')
TRANSIENT_ARXIV_STATUS_CODES = {429, 500, 502, 503, 504}
TRACKED_PAPER_REFRESH_CHUNK_SIZE = 10
TRACKED_PAPER_REFRESH_CHUNK_DELAY_SECONDS = 5
TRACKED_PAPER_REFRESH_RETRY_DELAYS_SECONDS = (60, 300)


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
    Fetches and normalizes paper information from arXiv and Crossref.
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
        # Crossref calls run in executor threads, so pacing needs a lock.
        self._crossref_lock = threading.Lock()
        self._last_crossref_request = 0.0

    async def fetch_latest_papers(self) -> List[Paper]:
        """
        Fetches target-author papers submitted within the lookback window.

        The window deliberately overlaps previous runs: already-posted papers
        are filtered out by the registry, so a late or skipped arXiv mailing,
        or a few days with the bot offline, never cause a paper to be missed.
        """
        since = datetime.now(timezone.utc) - timedelta(days=self.settings.lookback_days)
        self.logger.info(
            f"Fetching target-author papers submitted since {since:%Y-%m-%d %H:%M} UTC "
            f"({self.settings.lookback_days}-day window)."
        )
        api_papers = await self._fetch_from_api(since)
        # The API's au: search is looser than our matcher, so keep only papers
        # where a tracked author really appears.
        papers = [paper for paper in api_papers if self._is_author_match(paper.authors)]
        self.logger.info(f"Found {len(papers)} papers by tracked authors.")
        return papers

    async def fetch_publication_updates(
        self,
        tracked_paper_ids: List[str],
        tracked_metadata: Optional[Dict[str, TrackedPaperMetadata]] = None,
        crossref_due_ids: Optional[Set[str]] = None,
    ) -> PublicationRefreshResult:
        """
        Refreshes tracked arXiv papers and returns the ones that now look published.

        `crossref_due_ids` limits the per-paper Crossref title searches to the
        papers whose lookup is due (see publication_schedule.py); None means all.

        arXiv metadata is treated as the primary source of truth because a DOI or
        journal reference appearing there is the clearest signal that the authors
        have linked the preprint to its journal publication. Crossref is used as a
        conservative fallback when arXiv metadata has not been updated yet.
        """
        if not tracked_paper_ids:
            return PublicationRefreshResult(updates=[], checked_ids=[], failed_ids=[], degraded=False)

        metadata_by_id = self._normalize_tracked_metadata(tracked_metadata or {})
        tracked_paper_fetch = await self._fetch_papers_by_ids(tracked_paper_ids)
        publication_updates: List[Paper] = []
        loop = asyncio.get_event_loop()
        crossref_stats = {"requests": 0}
        crossref_fallback_attempts = 0
        crossref_fallback_skips = 0
        crossref_checked_ids: List[str] = []

        def crossref_due(paper_id: str) -> bool:
            return crossref_due_ids is None or paper_id in crossref_due_ids

        if tracked_paper_fetch.failed_ids:
            refreshed_count = len(tracked_paper_fetch.requested_ids) - len(tracked_paper_fetch.failed_ids)
            self.logger.warning(
                "Publication metadata refresh completed with gaps: refreshed %d/%d tracked papers; "
                "%d could not be checked this run.",
                refreshed_count,
                len(tracked_paper_fetch.requested_ids),
                len(tracked_paper_fetch.failed_ids),
            )

        for paper in tracked_paper_fetch.papers:
            # If arXiv itself already exposes publication metadata, prefer that
            # over any external inference.
            if paper.journal_ref or paper.doi:
                enriched = await self.enrich_publication_metadata(paper, crossref_stats=crossref_stats)
                publication_updates.append(enriched._replace(announce_type='published_metadata'))
                continue

            # Crossref is only consulted for papers that still look unpublished
            # on arXiv, which helps keep false positives low.
            if not crossref_due(paper.id):
                continue
            crossref_stats["requests"] += 1
            crossref_checked_ids.append(paper.id)
            crossref_match = await loop.run_in_executor(None, self._find_crossref_publication, paper)
            if crossref_match:
                publication_updates.append(crossref_match)

        for failed_id in tracked_paper_fetch.failed_ids:
            stored_metadata = metadata_by_id.get(failed_id)
            if stored_metadata is None:
                crossref_fallback_skips += 1
                continue
            if not crossref_due(failed_id):
                continue

            crossref_fallback_attempts += 1
            crossref_checked_ids.append(failed_id)
            crossref_match = await self._crossref_fallback_for_failed_refresh(
                failed_id,
                stored_metadata,
                crossref_stats=crossref_stats,
            )
            if crossref_match:
                publication_updates.append(crossref_match)

        checked_ids = [
            paper.id
            for paper in tracked_paper_fetch.papers
        ]
        if tracked_paper_fetch.failed_ids or crossref_stats["requests"]:
            suffix = ""
            if crossref_fallback_skips:
                suffix = (
                    f"; {crossref_fallback_skips} failed arXiv refreshes lacked enough "
                    "stored metadata for Crossref fallback"
                )
            self.logger.info(
                "Publication update Crossref summary: %d requests issued; "
                "Crossref-only fallback attempted for %d failed arXiv refreshes%s.",
                crossref_stats["requests"],
                crossref_fallback_attempts,
                suffix,
            )
        return PublicationRefreshResult(
            updates=publication_updates,
            checked_ids=checked_ids,
            failed_ids=tracked_paper_fetch.failed_ids,
            degraded=tracked_paper_fetch.degraded,
            crossref_checked_ids=crossref_checked_ids,
        )

    async def fetch_papers_by_ids(self, paper_ids: List[str]) -> FetchPapersByIdResult:
        """Fetches arXiv metadata for specific paper IDs."""
        return await self._fetch_papers_by_ids(paper_ids)

    async def enrich_publication_metadata(
        self,
        paper: Paper,
        crossref_stats: Optional[Dict[str, int]] = None,
    ) -> Paper:
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
            if crossref_stats is not None:
                crossref_stats["requests"] = crossref_stats.get("requests", 0) + 1
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
            if crossref_stats is not None:
                crossref_stats["requests"] = crossref_stats.get("requests", 0) + 1
            crossref_match = await loop.run_in_executor(None, self._find_crossref_publication, normalized_paper)
            if crossref_match:
                return crossref_match

        return normalized_paper

    async def _crossref_fallback_for_failed_refresh(
        self,
        paper_id: str,
        metadata: TrackedPaperMetadata,
        crossref_stats: Optional[Dict[str, int]] = None,
    ) -> Optional[Paper]:
        """Uses stored local metadata to query Crossref when arXiv refresh could not run."""
        fallback_paper = self._paper_from_tracked_metadata(paper_id, metadata)
        if fallback_paper is None:
            return None

        if fallback_paper.doi or fallback_paper.journal_ref:
            enriched = await self.enrich_publication_metadata(fallback_paper, crossref_stats=crossref_stats)
            if enriched.doi or enriched.journal_ref:
                return enriched._replace(announce_type='crossref_registry_fallback')
            return None

        loop = asyncio.get_event_loop()
        if crossref_stats is not None:
            crossref_stats["requests"] = crossref_stats.get("requests", 0) + 1
        return await loop.run_in_executor(None, self._find_crossref_publication, fallback_paper)

    def _normalize_tracked_metadata(
        self,
        tracked_metadata: Dict[str, TrackedPaperMetadata],
    ) -> Dict[str, TrackedPaperMetadata]:
        """Canonicalizes tracked-paper metadata keys so they match normalized arXiv IDs."""
        normalized: Dict[str, TrackedPaperMetadata] = {}
        for paper_id, metadata in tracked_metadata.items():
            normalized[canonicalize_paper_id(paper_id)] = TrackedPaperMetadata(
                title=self._clean_optional_text(metadata.title),
                authors=[
                    author.strip()
                    for author in metadata.authors
                    if isinstance(author, str) and author.strip()
                ],
                doi=self._clean_optional_text(metadata.doi),
                journal_ref=self._clean_optional_text(metadata.journal_ref),
            )
        return normalized

    def _paper_from_tracked_metadata(
        self,
        paper_id: str,
        metadata: TrackedPaperMetadata,
    ) -> Optional[Paper]:
        """Builds a synthetic Paper from locally stored metadata for Crossref fallback."""
        if not metadata.doi and (not metadata.title or not metadata.authors):
            return None

        return Paper(
            id=paper_id,
            title=metadata.title or f"arXiv:{paper_id}",
            authors=list(metadata.authors),
            published=datetime.now(ZoneInfo("UTC")),
            summary="",
            link=f"https://arxiv.org/abs/{paper_id}",
            pdf_link=f"https://arxiv.org/pdf/{paper_id}",
            doi=metadata.doi,
            journal_ref=metadata.journal_ref,
            announce_type='crossref_registry_seed',
        )

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

    async def _fetch_papers_by_ids(self, paper_ids: List[str]) -> FetchPapersByIdResult:
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
            return FetchPapersByIdResult(papers=[], requested_ids=[], failed_ids=[], degraded=False)

        loop = asyncio.get_event_loop()
        # Rebuild results in the original order later so the caller gets stable,
        # predictable processing independent of API response order.
        papers_by_id: Dict[str, Paper] = {}
        failed_ids: List[str] = []
        degraded = False
        id_chunks = [
            normalized_ids[i:i + TRACKED_PAPER_REFRESH_CHUNK_SIZE]
            for i in range(0, len(normalized_ids), TRACKED_PAPER_REFRESH_CHUNK_SIZE)
        ]

        for index, id_chunk in enumerate(id_chunks, start=1):
            self.logger.info(
                f"Refreshing publication metadata for chunk {index}/{len(id_chunks)} "
                f"({len(id_chunk)} tracked papers)."
            )
            search = arxiv.Search(id_list=id_chunk, max_results=len(id_chunk))

            chunk_results = []
            chunk_succeeded = False
            max_attempts = len(TRACKED_PAPER_REFRESH_RETRY_DELAYS_SECONDS) + 1
            for attempt in range(1, max_attempts + 1):
                try:
                    results_iterator = self.arxiv_client.results(search)
                    chunk_results = await loop.run_in_executor(None, list, results_iterator)
                    chunk_succeeded = True
                    break
                except Exception as e:
                    retryable = self._is_retryable_arxiv_error(e)
                    is_last_attempt = attempt == max_attempts

                    if retryable and not is_last_attempt:
                        backoff_seconds = TRACKED_PAPER_REFRESH_RETRY_DELAYS_SECONDS[attempt - 1]
                        self.logger.warning(
                            "Transient arXiv error while refreshing tracked papers for chunk %d/%d "
                            "(attempt %d/%d): %s. Cooling down for %d seconds before retrying.",
                            index,
                            len(id_chunks),
                            attempt,
                            max_attempts,
                            self._summarize_exception(e),
                            backoff_seconds,
                        )
                        await asyncio.sleep(backoff_seconds)
                        continue

                    degraded = True
                    failed_ids.extend(id_chunk)
                    log_method = self.logger.warning if retryable else self.logger.error
                    log_method(
                        "Could not refresh tracked-paper metadata for chunk %d/%d after %d attempt(s). "
                        "Skipping %d tracked papers for this run. Error: %s",
                        index,
                        len(id_chunks),
                        attempt,
                        len(id_chunk),
                        self._summarize_exception(e),
                        exc_info=True,
                    )

                    if retryable and index < len(id_chunks):
                        remaining_ids = [
                            paper_id
                            for remaining_chunk in id_chunks[index:]
                            for paper_id in remaining_chunk
                        ]
                        failed_ids.extend(remaining_ids)
                        self.logger.warning(
                            "Aborting the remaining %d tracked-paper refresh chunk(s) for this run "
                            "to avoid hammering the arXiv API.",
                            len(id_chunks) - index,
                        )
                        return FetchPapersByIdResult(
                            papers=[papers_by_id[paper_id] for paper_id in normalized_ids if paper_id in papers_by_id],
                            requested_ids=normalized_ids,
                            failed_ids=self._dedupe_preserving_order(failed_ids),
                            degraded=True,
                        )
                    break

            if not chunk_succeeded:
                continue

            for result in chunk_results:
                paper = self._normalize_api_result(result)
                papers_by_id[paper.id] = paper

            missing_ids = [
                paper_id
                for paper_id in id_chunk
                if paper_id not in papers_by_id
            ]
            if missing_ids:
                degraded = True
                failed_ids.extend(missing_ids)
                self.logger.warning(
                    "arXiv returned metadata for %d/%d tracked papers in chunk %d/%d. "
                    "Missing IDs: %s",
                    len(id_chunk) - len(missing_ids),
                    len(id_chunk),
                    index,
                    len(id_chunks),
                    ", ".join(missing_ids[:5]) + (" ..." if len(missing_ids) > 5 else ""),
                )

            if index < len(id_chunks):
                await asyncio.sleep(TRACKED_PAPER_REFRESH_CHUNK_DELAY_SECONDS)

        return FetchPapersByIdResult(
            papers=[papers_by_id[paper_id] for paper_id in normalized_ids if paper_id in papers_by_id],
            requested_ids=normalized_ids,
            failed_ids=self._dedupe_preserving_order(failed_ids),
            degraded=degraded,
        )

    def _extract_http_status_code(self, error: Exception) -> Optional[int]:
        """Extracts an HTTP status code from an arxiv.py exception message when present."""
        match = HTTP_STATUS_PATTERN.search(str(error))
        if not match:
            return None
        return int(match.group(1))

    def _is_retryable_arxiv_error(self, error: Exception) -> bool:
        """Returns True for transient arXiv API failures worth retrying later."""
        status_code = self._extract_http_status_code(error)
        if status_code in TRANSIENT_ARXIV_STATUS_CODES:
            return True

        error_text = str(error).lower()
        transient_markers = (
            "timed out",
            "temporarily unavailable",
            "connection aborted",
            "connection reset",
            "remote end closed connection",
        )
        return any(marker in error_text for marker in transient_markers)

    def _summarize_exception(self, error: Exception) -> str:
        """Builds a short one-line summary for logs without dropping the traceback."""
        status_code = self._extract_http_status_code(error)
        if status_code is not None:
            return f"HTTP {status_code}"
        return f"{type(error).__name__}: {error}"

    def _dedupe_preserving_order(self, values: List[str]) -> List[str]:
        """Removes duplicates from a list while preserving the original order."""
        seen = set()
        deduped: List[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped

    def _is_author_match(self, paper_authors: List[str]) -> bool:
        """
        Checks if any author in the paper's author list matches any of the target authors
        defined in the settings (case-insensitive).

        Args:
            paper_authors: A list of author names from a single paper.

        Returns:
            True if there is at least one match, False otherwise.
        """
        return bool(find_matching_target_authors(paper_authors, self.settings.target_authors))

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
            payload = self._crossref_get_json(request)
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
            payload = self._crossref_get_json(request)
        except Exception as e:
            self.logger.warning(f"Crossref DOI lookup failed for {doi}: {e}")
            return None

        item = payload.get("message")
        return item if isinstance(item, dict) else None

    def _crossref_get_json(self, request: urllib.request.Request) -> Any:
        """
        Performs one Crossref request, paced to at most one per second.

        On HTTP 429 it waits as long as Crossref asks (capped) and retries once;
        any other failure is raised to the caller.
        """
        for attempt in range(2):
            with self._crossref_lock:
                wait = self._last_crossref_request + CROSSREF_MIN_INTERVAL_SECONDS - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last_crossref_request = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    return json.load(response)
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 1:
                    raise
                retry_after = self._retry_after_seconds(e)
                self.logger.info(f"Crossref rate limit hit; waiting {retry_after:.0f}s before retrying.")
                time.sleep(retry_after)

    def _retry_after_seconds(self, error: urllib.error.HTTPError) -> float:
        try:
            seconds = float(error.headers.get("Retry-After", 5))
        except (TypeError, ValueError):
            seconds = 5.0
        return min(max(seconds, 1.0), CROSSREF_MAX_RETRY_AFTER_SECONDS)

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
