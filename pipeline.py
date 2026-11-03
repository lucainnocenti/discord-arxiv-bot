"""One bot run, independent of Discord: find new papers by tracked authors,
announce them, then announce tracked papers that have since been published.

Messages go out through a `send` coroutine supplied by the caller (the
webhook sender in production, a fake in tests).
"""

import logging
from typing import Awaitable, Callable, NamedTuple, Optional

from discord_formatter import format_new_paper, format_publication
from models import Paper
from new_papers import NewPaperFinder
from publication_check import PublicationChecker
from registry import PaperRegistry
from settings import AppSettings

# Delivers one message; returns True once it was sent.
SendFn = Callable[[str], Awaitable[bool]]

logger = logging.getLogger(__name__)


class RunSummary(NamedTuple):
    papers_posted: int
    publication_updates: int


class Pipeline:
    def __init__(
        self,
        settings: AppSettings,
        registry: PaperRegistry,
        new_papers: NewPaperFinder,
        publications: PublicationChecker,
        send: Optional[SendFn],  # may be None on --nosend dry runs
    ):
        self.settings = settings
        self.registry = registry
        self.new_papers = new_papers
        self.publications = publications
        self.send = send

    async def run(self) -> RunSummary:
        posted = await self._post_new_papers()
        # Runs every time, whatever happened above.
        published = await self._post_publication_updates()
        summary = RunSummary(papers_posted=posted, publication_updates=published)
        logger.info(
            f"Run finished: posted {summary.papers_posted} new paper(s) and "
            f"{summary.publication_updates} publication update(s)."
        )
        return summary

    async def _post_new_papers(self) -> int:
        try:
            papers, covered = await self.new_papers.find()
        except Exception as e:
            logger.exception(f"Failed to fetch papers: {e}")
            return 0

        posted = send_failures = 0
        for paper in papers:
            # The fetch window overlaps earlier runs, so most papers here were
            # already posted; check that before spending any Crossref calls.
            if self.registry.get(paper.id).posted:
                logger.info(f"Paper {paper.id} was already posted in a previous run, skipping.")
                continue

            # Enrich before posting so the initial announcement already
            # includes DOI/journal data when arXiv or Crossref has it.
            paper = await self.publications.enrich(paper)
            delivered = await self._announce(paper, format_new_paper(paper, self.settings.authors))
            if delivered:
                posted += 1
                self.registry.record_paper(paper, posted=True, published=bool(paper.journal_ref or paper.doi))
                # Save right away: a crash later in the run must not lose the
                # fact that this paper was announced, or it would be re-posted.
                self.registry.save()
            elif delivered is False:
                send_failures += 1

        # A paper that failed to send may drop out of tomorrow's feed, so the
        # announcement is only recorded as processed when everything went out;
        # otherwise the next run's API search picks the paper up again.
        if send_failures:
            logger.warning(f"{send_failures} message(s) failed to send; they will be retried next run.")
        elif covered and not self.settings.no_send:
            self.new_papers.record_coverage(covered)
        return posted

    async def _post_publication_updates(self) -> int:
        """Announces tracked preprints that now have journal publication metadata."""
        tracked = self.registry.awaiting_publication()
        if not tracked:
            logger.info("No tracked unpublished papers require publication checks.")
            return 0

        logger.info(f"Checking publication status for {len(tracked)} tracked papers.")
        try:
            updates = await self.publications.find_updates(tracked)
        except Exception as e:
            logger.exception(f"Failed to check publication status: {e}")
            return 0

        announced = 0
        for paper in updates:
            # The flag is flipped only after a successful send, so each
            # publication is announced exactly once.
            if await self._announce(paper, format_publication(paper, self.settings.authors)):
                announced += 1
                self.registry.record_paper(paper, published=True)
                self.registry.save()
        return announced

    async def _announce(self, paper: Paper, message: Optional[str]) -> Optional[bool]:
        """
        Sends one formatted message. Returns True if it was delivered, False
        if sending failed, and None if it was skipped (unformattable, or a
        --nosend dry run).
        """
        if not message:
            logger.warning(f"Skipping message for '{paper.title}': formatting failed (likely too long).")
            return None

        if self.settings.no_send:
            logger.info(f"[NO_SEND] Would post message for paper: {paper.title}")
            logger.debug(f"Message content:\n{message}")
            return None

        logger.info(f"Sending message for paper: {paper.title}")
        return await self.send(message)
