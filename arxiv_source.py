"""Talks to the arXiv API: finds recent papers by tracked authors and refreshes
metadata for already-known papers."""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import arxiv # Library for interacting with the arXiv API

from models import FetchPapersByIdResult, Paper, canonicalize_paper_id, clean_optional_text
from settings import AppSettings
from utils import find_matching_target_authors

HTTP_STATUS_PATTERN = re.compile(r'HTTP (\d{3})')
TRANSIENT_ARXIV_STATUS_CODES = {429, 500, 502, 503, 504}
# Author searches are split so the query string stays within the size arXiv
# accepts and a failure affects only one chunk.
MAX_AUTHORS_PER_QUERY = 20
TRACKED_PAPER_REFRESH_CHUNK_SIZE = 10
TRACKED_PAPER_REFRESH_CHUNK_DELAY_SECONDS = 5
TRACKED_PAPER_REFRESH_RETRY_DELAYS_SECONDS = (60, 300)


class ArxivSource:
    def __init__(self, settings: AppSettings):
        self.settings = settings
        # page_size, delay_seconds, num_retries help manage API rate limits and transient errors.
        self.client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=3)
        self.logger = logging.getLogger(self.__class__.__name__)

    async def search_recent(self) -> List[Paper]:
        """
        Fetches tracked-author papers submitted within the lookback window.

        The window deliberately overlaps previous runs: already-posted papers
        are filtered out by the registry, so a late or skipped arXiv mailing,
        or a few days with the bot offline, never cause a paper to be missed.
        """
        since = datetime.now(timezone.utc) - timedelta(days=self.settings.lookback_days)
        self.logger.info(
            f"Fetching target-author papers submitted since {since:%Y-%m-%d %H:%M} UTC "
            f"({self.settings.lookback_days}-day window)."
        )
        api_papers = await self._search_since(since)
        # The API's au: search is looser than our matcher, so keep only papers
        # where a tracked author really appears.
        papers = [paper for paper in api_papers if self.is_author_match(paper.authors)]
        self.logger.info(f"Found {len(papers)} papers by tracked authors.")
        return papers

    def is_author_match(self, paper_authors: List[str]) -> bool:
        """True if any tracked author (or one of their name variants) wrote the paper."""
        return bool(find_matching_target_authors(paper_authors, self.settings.target_authors))

    async def fetch_by_ids(self, paper_ids: List[str]) -> FetchPapersByIdResult:
        """Fetches specific arXiv papers by ID to refresh their metadata."""
        normalized_ids = _dedupe_preserving_order(canonicalize_paper_id(paper_id) for paper_id in paper_ids)
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

        def result(degraded_flag: bool) -> FetchPapersByIdResult:
            return FetchPapersByIdResult(
                papers=[papers_by_id[paper_id] for paper_id in normalized_ids if paper_id in papers_by_id],
                requested_ids=normalized_ids,
                failed_ids=_dedupe_preserving_order(failed_ids),
                degraded=degraded_flag,
            )

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
                    results_iterator = self.client.results(search)
                    chunk_results = await loop.run_in_executor(None, list, results_iterator)
                    chunk_succeeded = True
                    break
                except Exception as e:
                    retryable = _is_retryable_arxiv_error(e)
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
                            _summarize_exception(e),
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
                        _summarize_exception(e),
                        exc_info=True,
                    )

                    if retryable and index < len(id_chunks):
                        failed_ids.extend(
                            paper_id
                            for remaining_chunk in id_chunks[index:]
                            for paper_id in remaining_chunk
                        )
                        self.logger.warning(
                            "Aborting the remaining %d tracked-paper refresh chunk(s) for this run "
                            "to avoid hammering the arXiv API.",
                            len(id_chunks) - index,
                        )
                        return result(True)
                    break

            if not chunk_succeeded:
                continue

            for api_result in chunk_results:
                paper = _paper_from_api_result(api_result)
                papers_by_id[paper.id] = paper

            missing_ids = [paper_id for paper_id in id_chunk if paper_id not in papers_by_id]
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

        return result(degraded)

    async def _search_since(self, since: datetime) -> List[Paper]:
        """Runs the category + author + submission-date search, chunked by author."""
        # arXiv's submittedDate filter takes YYYYMMDDHHMMSS in UTC.
        date_query_str = since.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")

        all_authors = self.settings.target_authors
        # The API query is more reliable with full names than with bare
        # initials, so skip low-signal variants when a better form exists.
        authors = _dedupe_preserving_order(
            author for author in all_authors
            if len(author.split()) >= 2 and len(author.split()[0]) > 1 and not author.split()[0].endswith('.')
        ) or list(all_authors)
        if len(authors) != len(all_authors):
            self.logger.info(
                f"Using {len(authors)} canonical author names for API query "
                f"(filtered from {len(all_authors)} configured names)."
            )

        author_chunks: List[List[str]] = [
            authors[i:i + MAX_AUTHORS_PER_QUERY]
            for i in range(0, len(authors), MAX_AUTHORS_PER_QUERY)
        ] or [[]]

        loop = asyncio.get_event_loop()
        unique_results: List[arxiv.Result] = []
        seen_entry_ids = set()

        for index, author_chunk in enumerate(author_chunks, start=1):
            query = f'cat:{self.settings.category} AND submittedDate:[{date_query_str} TO 99991231235959]'
            if author_chunk:
                authors_query = ' OR '.join(f'au:"{author}"' for author in author_chunk)
                query = f'cat:{self.settings.category} AND ({authors_query}) AND submittedDate:[{date_query_str} TO 99991231235959]'

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
                    results_iterator = self.client.results(search)
                    chunk_results = await loop.run_in_executor(None, list, results_iterator)
                    self.logger.info(
                        f"arXiv API chunk {index}/{len(author_chunks)} returned {len(chunk_results)} results."
                    )
                    break
                except Exception as e:
                    if "HTTP 429" in str(e) and attempt < 2:
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

            for api_result in chunk_results:
                # Multiple author chunks can surface the same paper, so dedupe
                # before normalization to keep downstream posting stable.
                if api_result.entry_id in seen_entry_ids:
                    continue
                seen_entry_ids.add(api_result.entry_id)
                unique_results.append(api_result)

            if index < len(author_chunks):
                await asyncio.sleep(3)

        unique_results.sort(key=lambda r: r.published)
        return [_paper_from_api_result(api_result) for api_result in unique_results]


def _paper_from_api_result(result: arxiv.Result) -> Paper:
    """Converts an arxiv.Result into a Paper."""
    return Paper(
        id=canonicalize_paper_id(result.entry_id),
        title=result.title.strip(),
        authors=[author.name for author in result.authors],
        published=result.published, # Timezone-aware (UTC)
        summary=result.summary.strip().replace('\n', ' '),
        link=result.entry_id, # The abstract page URL
        pdf_link=f"http://arxiv.org/pdf/{result.get_short_id()}",
        doi=clean_optional_text(result.doi), # Present once authors link a journal version
        journal_ref=clean_optional_text(result.journal_ref),
        announce_type='api_new',
    )


def _extract_http_status_code(error: Exception) -> Optional[int]:
    """Extracts an HTTP status code from an arxiv.py exception message when present."""
    match = HTTP_STATUS_PATTERN.search(str(error))
    return int(match.group(1)) if match else None


def _is_retryable_arxiv_error(error: Exception) -> bool:
    """Returns True for transient arXiv API failures worth retrying later."""
    if _extract_http_status_code(error) in TRANSIENT_ARXIV_STATUS_CODES:
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


def _summarize_exception(error: Exception) -> str:
    """Builds a short one-line summary for logs without dropping the traceback."""
    status_code = _extract_http_status_code(error)
    if status_code is not None:
        return f"HTTP {status_code}"
    return f"{type(error).__name__}: {error}"


def _dedupe_preserving_order(values) -> List[str]:
    seen = set()
    deduped: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped
