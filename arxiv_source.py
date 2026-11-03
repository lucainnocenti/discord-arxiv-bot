"""Talks to arXiv: finds recent papers by tracked authors (RSS feed for each
day, API search for missed days) and refreshes metadata for known papers."""

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple

import arxiv # Library for interacting with the arXiv API

from models import Paper, canonicalize_paper_id, clean_optional_text
from notifier import send_telegram_message
from rss_source import RssFeed, fetch_rss_feed, previous_announcement_day
from settings import AppSettings
from tracked_authors import find_matching_target_authors
from utils import read_date, write_date

HTTP_STATUS_PATTERN = re.compile(r'HTTP (\d{3})')
TRANSIENT_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
TRANSIENT_ERROR_MARKERS = (
    "timed out",
    "temporarily unavailable",
    "connection aborted",
    "connection reset",
    "remote end closed connection",
)
# The arxiv client already retries a failed page a few times in quick
# succession; these longer waits ride out rate limiting and short outages.
RETRY_DELAYS_SECONDS = (60, 300)
# Pause between consecutive API requests, as arXiv asks of API clients.
CHUNK_DELAY_SECONDS = 3
# Author searches are split so the query string stays within the size arXiv
# accepts; ID lookups are split to keep each request small.
MAX_AUTHORS_PER_QUERY = 20
MAX_IDS_PER_QUERY = 10
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

    async def search_recent(self) -> Tuple[List[Paper], Optional[date]]:
        """
        Fetches recent papers by tracked authors.

        Each day's papers come from the RSS feed of the latest announcement.
        The API search fills gaps: it runs when the feed is empty or unreadable,
        or when announcements were missed since the last one processed (bot
        offline, a skipped mailing, a failed run). Its window starts a few days
        before that last announcement and overlaps what was already posted;
        the registry filters out duplicates.

        Returns the papers, and the announcement day to pass to
        record_coverage() once they are all posted (None if something may
        still be missing, so the next run searches the gap again).
        """
        feed = await self._read_feed()
        candidates: List[Paper] = list(feed.papers) if feed else []
        feed_has_papers = bool(feed and feed.papers and feed.announced)
        last_covered = read_date(self.settings.rss_coverage_file)

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
                    f"{gap}. Falling back to the arXiv API for {', '.join(self.settings.categories)} papers "
                    f"submitted since {since:%Y-%m-%d %H:%M} UTC.",
                )
            api_papers, api_complete = await self._search_since(since)
            known_ids = {paper.id for paper in candidates}
            candidates += [paper for paper in api_papers if paper.id not in known_ids]
        else:
            self.logger.info(f"The RSS feed follows the last processed announcement ({last_covered}); no API search needed.")

        # The announcement counts as processed only if the feed had it and any
        # gap before it was fully searched.
        covered = None
        if feed_has_papers and (not gap or (not self.settings.rss_only and api_complete)):
            covered = feed.announced

        # Both sources return more than we want (the API's au: search is loose,
        # the feed lists everyone), so keep only papers by a tracked author.
        papers = [paper for paper in candidates if self.is_author_match(paper.authors)]
        self.logger.info(f"Found {len(papers)} papers by tracked authors.")
        return papers, covered

    def record_coverage(self, day: date):
        """Records the announcement `day` from search_recent() as processed."""
        if self.settings.no_save:
            self.logger.info(f"Not recording announcement {day} as processed (--nosave).")
            return
        write_date(self.settings.rss_coverage_file, day)
        self.logger.info(f"Recorded announcement {day} as processed.")

    def is_author_match(self, paper_authors: List[str]) -> bool:
        """True if any tracked author (or one of their name variants) wrote the paper."""
        return bool(find_matching_target_authors(paper_authors, self.settings.target_authors))

    async def fetch_by_ids(self, paper_ids: List[str]) -> Tuple[List[Paper], List[str]]:
        """
        Fetches specific arXiv papers by ID to refresh their metadata. Returns
        the papers found and the IDs that could not be fetched this run.
        """
        ids = list(dict.fromkeys(canonicalize_paper_id(paper_id) for paper_id in paper_ids))
        papers: List[Paper] = []
        for start in range(0, len(ids), MAX_IDS_PER_QUERY):
            chunk = ids[start:start + MAX_IDS_PER_QUERY]
            if start:
                await asyncio.sleep(CHUNK_DELAY_SECONDS)
            try:
                results = await self._results(arxiv.Search(id_list=chunk, max_results=len(chunk)))
            except Exception as e:
                # Retries are exhausted: arXiv is struggling, so leave the
                # remaining chunks for the next run instead of hammering it.
                self.logger.error(f"Stopping the arXiv refresh of tracked papers: {_summarize(e)}", exc_info=True)
                break
            papers += [_paper_from_api_result(result) for result in results]

        found = {paper.id for paper in papers}
        failed_ids = [paper_id for paper_id in ids if paper_id not in found]
        if failed_ids:
            self.logger.warning(
                f"Could not refresh {len(failed_ids)}/{len(ids)} tracked papers from arXiv this run: "
                + ", ".join(failed_ids[:5]) + (" ..." if len(failed_ids) > 5 else "")
            )
        return papers, failed_ids

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

    async def _search_since(self, since: datetime) -> Tuple[List[Paper], bool]:
        """
        Runs the category + author + submission-date search, chunked by author.
        Returns the papers found and whether every chunk succeeded.
        """
        # arXiv's submittedDate filter takes YYYYMMDDHHMMSS in UTC.
        date_filter = f"submittedDate:[{since.astimezone(timezone.utc):%Y%m%d%H%M%S} TO 99991231235959]"
        authors = self._api_author_names()
        results = {}
        complete = True
        for start in range(0, len(authors), MAX_AUTHORS_PER_QUERY):
            if start:
                await asyncio.sleep(CHUNK_DELAY_SECONDS)
            authors_query = ' OR '.join(f'au:"{author}"' for author in authors[start:start + MAX_AUTHORS_PER_QUERY])
            search = arxiv.Search(
                query=f'{_category_query(self.settings.categories)} AND ({authors_query}) AND {date_filter}',
                max_results=self.settings.max_results,
                sort_by=arxiv.SortCriterion.SubmittedDate,
                sort_order=arxiv.SortOrder.Ascending,
            )
            try:
                chunk_results = await self._results(search)
            except Exception as e:
                self.logger.error(f"Stopping the arXiv API search: {_summarize(e)}", exc_info=True)
                complete = False
                break
            # Several author chunks can return the same paper.
            for result in chunk_results:
                results.setdefault(result.entry_id, result)

        self.logger.info(f"The arXiv API search returned {len(results)} papers.")
        return [_paper_from_api_result(result) for result in sorted(results.values(), key=lambda r: r.published)], complete

    def _api_author_names(self) -> List[str]:
        """The tracked names worth querying: the API search is more reliable
        with full names than with bare initials, which the matcher covers anyway."""
        all_authors = self.settings.target_authors
        full_names = [
            author for author in all_authors
            if len(author.split()) >= 2 and len(author.split()[0]) > 1 and not author.split()[0].endswith('.')
        ]
        return list(dict.fromkeys(full_names or all_authors))

    async def _results(self, search: arxiv.Search) -> List[arxiv.Result]:
        """Runs one API request, waiting and retrying after transient errors."""
        for delay in (*RETRY_DELAYS_SECONDS, None):
            try:
                return await asyncio.to_thread(lambda: list(self.client.results(search)))
            except Exception as e:
                if delay is None or not _is_transient(e):
                    raise
                self.logger.warning(f"Transient arXiv error ({_summarize(e)}); retrying in {delay}s.")
                await asyncio.sleep(delay)


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
    )


def _category_query(categories: List[str]) -> str:
    """The API filter for papers in any of the categories, e.g.
    (cat:quant-ph* OR cat:cond-mat*). An archive with subcategories, such as
    cond-mat, only matches with a wildcard: papers are filed under
    cond-mat.mes-hall etc., never plain cond-mat."""
    terms = [f"cat:{category}" if "." in category else f"cat:{category}*" for category in categories]
    return terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"


def _http_status(error: Exception) -> Optional[int]:
    """The HTTP status code in an arxiv.py exception message, if any."""
    match = HTTP_STATUS_PATTERN.search(str(error))
    return int(match.group(1)) if match else None


def _is_transient(error: Exception) -> bool:
    """True for arXiv API failures worth retrying after a wait."""
    if _http_status(error) in TRANSIENT_HTTP_STATUS_CODES:
        return True
    error_text = str(error).lower()
    return any(marker in error_text for marker in TRANSIENT_ERROR_MARKERS)


def _summarize(error: Exception) -> str:
    """A short one-line description of an error for log messages."""
    status = _http_status(error)
    return f"HTTP {status}" if status is not None else f"{type(error).__name__}: {error}"
