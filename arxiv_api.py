"""The arXiv API: searching recent papers by author and fetching known papers
by ID, with longer retries for arXiv's frequent temporary errors."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import arxiv # Library for interacting with the arXiv API

from models import Paper, canonicalize_paper_id, clean_optional_text

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
REQUEST_DELAY_SECONDS = 3
# Author searches are split so the query string stays within the size arXiv
# accepts; ID lookups are split to keep each request small.
MAX_AUTHORS_PER_QUERY = 20
MAX_IDS_PER_QUERY = 10
MAX_SEARCH_RESULTS = 1000

logger = logging.getLogger(__name__)


class ArxivApi:
    def __init__(self):
        # page_size, delay_seconds, num_retries help manage API rate limits and transient errors.
        self.client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=3)

    async def search(self, categories: List[str], author_names: List[str], since: datetime) -> Tuple[List[Paper], bool]:
        """
        Finds papers in `categories` submitted since `since` that list one of
        `author_names`, oldest first. arXiv's author search is loose, so
        callers should still match the authors themselves.

        Returns the papers found and whether the whole search succeeded; on a
        failure it stops early and returns what it found so far.
        """
        # arXiv's submittedDate filter takes YYYYMMDDHHMMSS in UTC.
        date_filter = f"submittedDate:[{since.astimezone(timezone.utc):%Y%m%d%H%M%S} TO 99991231235959]"
        names = _query_names(author_names)
        results: Dict[str, arxiv.Result] = {}
        complete = True
        for start in range(0, len(names), MAX_AUTHORS_PER_QUERY):
            if start:
                await asyncio.sleep(REQUEST_DELAY_SECONDS)
            authors_query = ' OR '.join(f'au:"{name}"' for name in names[start:start + MAX_AUTHORS_PER_QUERY])
            search = arxiv.Search(
                query=f'{category_query(categories)} AND ({authors_query}) AND {date_filter}',
                max_results=MAX_SEARCH_RESULTS,
                sort_by=arxiv.SortCriterion.SubmittedDate,
                sort_order=arxiv.SortOrder.Ascending,
            )
            try:
                chunk_results = await self._results(search)
            except Exception as e:
                logger.error(f"Stopping the arXiv API search: {_summarize(e)}", exc_info=True)
                complete = False
                break
            # Several author chunks can return the same paper.
            for result in chunk_results:
                results.setdefault(result.entry_id, result)

        logger.info(f"The arXiv API search returned {len(results)} papers.")
        ordered = sorted(results.values(), key=lambda result: result.published)
        return [_paper_from_result(result) for result in ordered], complete

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
                await asyncio.sleep(REQUEST_DELAY_SECONDS)
            try:
                results = await self._results(arxiv.Search(id_list=chunk, max_results=len(chunk)))
            except Exception as e:
                # Retries are exhausted: arXiv is struggling, so leave the
                # remaining chunks for the next run instead of hammering it.
                logger.error(f"Stopping the arXiv refresh of tracked papers: {_summarize(e)}", exc_info=True)
                break
            papers += [_paper_from_result(result) for result in results]

        found = {paper.id for paper in papers}
        failed_ids = [paper_id for paper_id in ids if paper_id not in found]
        if failed_ids:
            logger.warning(
                f"Could not refresh {len(failed_ids)}/{len(ids)} tracked papers from arXiv this run: "
                + ", ".join(failed_ids[:5]) + (" ..." if len(failed_ids) > 5 else "")
            )
        return papers, failed_ids

    async def _results(self, search: arxiv.Search) -> List[arxiv.Result]:
        """Runs one API request, waiting and retrying after transient errors."""
        for delay in (*RETRY_DELAYS_SECONDS, None):
            try:
                return await asyncio.to_thread(lambda: list(self.client.results(search)))
            except Exception as e:
                if delay is None or not _is_transient(e):
                    raise
                logger.warning(f"Transient arXiv error ({_summarize(e)}); retrying in {delay}s.")
                await asyncio.sleep(delay)


def category_query(categories: List[str]) -> str:
    """The API filter for papers in any of the categories, e.g.
    (cat:quant-ph* OR cat:cond-mat*). An archive with subcategories, such as
    cond-mat, only matches with a wildcard: papers are filed under
    cond-mat.mes-hall etc., never plain cond-mat."""
    terms = [f"cat:{category}" if "." in category else f"cat:{category}*" for category in categories]
    return terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"


def _query_names(author_names: List[str]) -> List[str]:
    """The names worth querying: the API search is more reliable with full
    names than with initials, and the caller's matcher covers initials anyway."""
    def is_full_name(name: str) -> bool:
        parts = name.split()
        return len(parts) >= 2 and len(parts[0]) > 1 and not parts[0].endswith('.')

    return list(dict.fromkeys([name for name in author_names if is_full_name(name)] or author_names))


def _paper_from_result(result: arxiv.Result) -> Paper:
    """Converts an arxiv.Result into a Paper."""
    return Paper(
        id=canonicalize_paper_id(result.entry_id),
        title=result.title.strip(),
        authors=[author.name for author in result.authors],
        submitted=result.published, # Timezone-aware (UTC)
        summary=result.summary.strip().replace('\n', ' '),
        link=result.entry_id, # The abstract page URL
        doi=clean_optional_text(result.doi), # Present once authors link a journal version
        journal_ref=clean_optional_text(result.journal_ref),
    )


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
