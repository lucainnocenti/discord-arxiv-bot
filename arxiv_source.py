"""Talks to arXiv: finds recent papers by tracked authors (RSS feed for each
day, API search for missed days) and refreshes metadata for known papers."""

import asyncio
import logging
import re
import socket
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import arxiv # Library for interacting with the arXiv API

from models import FetchPapersByIdResult, Paper, canonicalize_paper_id, clean_optional_text
from notifier import send_telegram_message
from rss_source import RssFeed, fetch_rss_feed, previous_announcement_day, read_last_covered, write_last_covered
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
# How far before the last processed announcement the gap-filling API search
# starts, and the furthest back it ever goes (e.g. after a long time offline).
API_WINDOW_MARGIN_DAYS = 5
MAX_CATCHUP_DAYS = 60


class ArxivSource:
    def __init__(self, settings: AppSettings):
        self.settings = settings
        # page_size, delay_seconds, num_retries help manage API rate limits and transient errors.
        self.client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=3)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._pending_coverage: Optional[date] = None

    async def search_recent(self) -> List[Paper]:
        """
        Fetches recent papers by tracked authors.

        Each day's papers come from the RSS feed of the latest announcement.
        The API search fills gaps: it runs when the feed is empty or unreadable,
        or when announcements were missed since the last one processed (bot
        offline, a skipped mailing, a failed run). Its window starts a few days
        before that last announcement and overlaps what was already posted;
        the registry filters out duplicates.

        After posting, call commit_coverage() to record the announcement as
        processed; it is only recorded when nothing was left uncovered.
        """
        self._pending_coverage = None
        feed = await self._read_feed()
        candidates: List[Paper] = list(feed.papers) if feed else []
        feed_has_papers = bool(feed and feed.papers and feed.announced)
        last_covered = read_last_covered(self.settings.rss_coverage_file)

        gap = self._coverage_gap(feed, last_covered)
        api_complete = True
        if gap and self.settings.rss_only:
            self.logger.warning(f"Not running the API search although {gap} (--rss).")
        elif gap:
            since = self._api_window_start(last_covered)
            self.logger.info(f"Running the API search because {gap}: papers submitted since {since:%Y-%m-%d %H:%M} UTC.")
            if not feed_has_papers:
                await asyncio.to_thread(
                    send_telegram_message,
                    self.settings,
                    f"arxiv-bot on {socket.gethostname()}: {gap}. "
                    f"Falling back to the arXiv API for {', '.join(self.settings.categories)} papers "
                    f"submitted since {since:%Y-%m-%d %H:%M} UTC.",
                )
            api_papers, api_complete = await self._search_since(since)
            known_ids = {paper.id for paper in candidates}
            candidates += [paper for paper in api_papers if paper.id not in known_ids]
        else:
            self.logger.info(f"The RSS feed follows the last processed announcement ({last_covered}); no API search needed.")

        # The announcement counts as processed only if the feed had it and any
        # gap before it was fully searched.
        if feed_has_papers and (not gap or (not self.settings.rss_only and api_complete)):
            self._pending_coverage = feed.announced

        # Both sources return more than we want (the API's au: search is loose,
        # the feed lists everyone), so keep only papers by a tracked author.
        papers = [paper for paper in candidates if self.is_author_match(paper.authors)]
        self.logger.info(f"Found {len(papers)} papers by tracked authors.")
        return papers

    def commit_coverage(self):
        """Records the announcement from the last search_recent() as processed."""
        if self._pending_coverage is None:
            return
        if self.settings.no_save:
            self.logger.info(f"Not recording announcement {self._pending_coverage} as processed (--nosave).")
            return
        write_last_covered(self.settings.rss_coverage_file, self._pending_coverage)
        self.logger.info(f"Recorded announcement {self._pending_coverage} as processed.")

    def _coverage_gap(self, feed: Optional[RssFeed], last_covered: Optional[date]) -> Optional[str]:
        """Why the API search is needed this run, or None if the feed suffices."""
        if feed is None:
            return "the RSS feed could not be read"
        if not feed.papers or not feed.announced:
            return "the RSS feed is empty"
        if self.settings.force_api:
            return "--api was given"
        if last_covered is None:
            return "no processed announcement is on record"
        if feed.announced > last_covered and previous_announcement_day(feed.announced) > last_covered:
            return f"announcements after {last_covered} and before {feed.announced} were not processed"
        return None

    def _api_window_start(self, last_covered: Optional[date]) -> datetime:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=self.settings.lookback_days)
        if last_covered:
            # Papers announced after `last_covered` were submitted at most a few
            # days before it (weekends); reach back far enough to include them.
            gap_start = datetime.combine(last_covered, time.min, timezone.utc) - timedelta(days=API_WINDOW_MARGIN_DAYS)
            since = min(since, gap_start)
        return max(since, now - timedelta(days=MAX_CATCHUP_DAYS))

    async def _read_feed(self) -> Optional[RssFeed]:
        try:
            return await asyncio.to_thread(fetch_rss_feed, self.settings.categories)
        except Exception as e:
            self.logger.error(f"Could not read the arXiv RSS feed: {type(e).__name__}: {e}")
            return None

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

    async def _search_since(self, since: datetime) -> Tuple[List[Paper], bool]:
        """
        Runs the category + author + submission-date search, chunked by author.
        Returns the papers found and whether every chunk succeeded.
        """
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

        category_query = _category_query(self.settings.categories)
        loop = asyncio.get_event_loop()
        unique_results: List[arxiv.Result] = []
        seen_entry_ids = set()
        complete = True

        for index, author_chunk in enumerate(author_chunks, start=1):
            query = f'{category_query} AND submittedDate:[{date_query_str} TO 99991231235959]'
            if author_chunk:
                authors_query = ' OR '.join(f'au:"{author}"' for author in author_chunk)
                query = f'{category_query} AND ({authors_query}) AND submittedDate:[{date_query_str} TO 99991231235959]'

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
                    complete = False
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
        return [_paper_from_api_result(api_result) for api_result in unique_results], complete


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


def _category_query(categories: List[str]) -> str:
    """The API filter for papers in any of the categories, e.g.
    (cat:quant-ph OR cat:cond-mat*). An archive with subcategories, such as
    cond-mat, only matches with a wildcard: papers are filed under
    cond-mat.mes-hall etc., never plain cond-mat."""
    terms = [f"cat:{category}" if "." in category else f"cat:{category}*" for category in categories]
    return terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"


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
