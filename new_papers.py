"""Finds the day's new papers by tracked authors.

Each day's papers come from the RSS feed of the latest announcement. The API
search fills gaps: it runs when the feed is empty or unreadable, or when
announcements were missed since the last one processed (bot offline, a
skipped mailing, a failed run). Its window starts a few days before that last
announcement and overlaps what was already posted; the registry filters out
duplicates.

The last announcement processed is kept in a file, and only advances once all
of its papers were posted and any gap before it was searched.
"""

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple

from arxiv_api import ArxivApi
from arxiv_rss import RssFeed, fetch_rss_feed
from models import Paper
from notifier import send_telegram_message
from settings import AppSettings
from utils import read_date, write_date

# How far before the last processed announcement the gap-filling API search
# starts, and the furthest back it ever goes (e.g. after a long time offline).
API_WINDOW_MARGIN_DAYS = 5
MAX_CATCHUP_DAYS = 60

logger = logging.getLogger(__name__)


class NewPaperFinder:
    def __init__(self, settings: AppSettings, api: ArxivApi):
        self.settings = settings
        self.api = api

    async def find(self) -> Tuple[List[Paper], Optional[date]]:
        """
        Returns the new papers by tracked authors, and the announcement day to
        pass to record_coverage() once they are all posted (None if something
        may still be missing, so the next run searches the gap again).
        """
        feed = await self._read_feed()
        candidates: List[Paper] = list(feed.papers) if feed else []
        feed_has_papers = bool(feed and feed.papers and feed.announced)
        last_covered = read_date(self.settings.rss_coverage_file)

        gap = self._coverage_gap(feed, last_covered)
        api_complete = True
        if gap and self.settings.rss_only:
            logger.warning(f"Not running the API search although {gap} (--rss).")
        elif gap:
            since = self._api_window_start(last_covered)
            logger.info(f"Running the API search because {gap}: papers submitted since {since:%Y-%m-%d %H:%M} UTC.")
            if not feed_has_papers:
                await asyncio.to_thread(
                    send_telegram_message,
                    self.settings,
                    f"{gap}. Falling back to the arXiv API for {', '.join(self.settings.categories)} papers "
                    f"submitted since {since:%Y-%m-%d %H:%M} UTC.",
                )
            api_papers, api_complete = await self.api.search(self.settings.categories, self.settings.authors.all_names(), since)
            known_ids = {paper.id for paper in candidates}
            candidates += [paper for paper in api_papers if paper.id not in known_ids]
        else:
            logger.info(f"The RSS feed follows the last processed announcement ({last_covered}); no API search needed.")

        # The announcement counts as processed only if the feed had it and any
        # gap before it was fully searched.
        covered = None
        if feed_has_papers and (not gap or (not self.settings.rss_only and api_complete)):
            covered = feed.announced

        # Both sources return more than we want (the API's au: search is loose,
        # the feed lists everyone), so keep only papers by a tracked author.
        papers = [paper for paper in candidates if self.settings.authors.found_in(paper.authors)]
        logger.info(f"Found {len(papers)} papers by tracked authors.")
        return papers, covered

    def record_coverage(self, day: date):
        """Records the announcement `day` from find() as processed."""
        if self.settings.no_save:
            logger.info(f"Not recording announcement {day} as processed (--nosave).")
            return
        write_date(self.settings.rss_coverage_file, day)
        logger.info(f"Recorded announcement {day} as processed.")

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
            logger.error(f"Could not read the arXiv RSS feed: {type(e).__name__}: {e}")
            return None


def previous_announcement_day(day: date) -> date:
    """The weekday before `day`: arXiv announces Monday to Friday."""
    previous = day - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous
