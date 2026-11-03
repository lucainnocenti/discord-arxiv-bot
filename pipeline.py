"""One bot run, independent of Discord: find new papers by tracked authors,
announce them, then announce tracked papers that have since been published.

Messages go out through a `send` coroutine supplied by the caller (the
Discord client in production, a fake in tests).
"""

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Set

from arxiv_source import ArxivSource
from discord_formatter import format_paper_message
from models import Paper, TrackedPaperMetadata
from publication_check import PublicationChecker
from registry import PaperRegistry
from settings import AppSettings

# Delivers one message; returns True once it was sent.
SendFn = Callable[[str], Awaitable[bool]]


@dataclass
class RunSummary:
    papers_posted: int = 0
    publication_updates: int = 0
    records_hydrated: int = 0
    # True when some tracked papers could not be refreshed this run.
    degraded: bool = False


class Pipeline:
    def __init__(
        self,
        settings: AppSettings,
        registry: PaperRegistry,
        arxiv_source: ArxivSource,
        publications: PublicationChecker,
        send: Optional[SendFn],  # may be None on --nosend dry runs
    ):
        self.settings = settings
        self.registry = registry
        self.arxiv = arxiv_source
        self.publications = publications
        self.send = send
        self.logger = logging.getLogger(self.__class__.__name__)
        # Guards against the same paper showing up twice within one run.
        self.announced_new: Set[str] = set()
        self.announced_published: Set[str] = set()

    async def run(self) -> RunSummary:
        summary = RunSummary()
        await self._post_new_papers(summary)
        await self._hydrate_missing_metadata(summary)
        await self._post_publication_updates(summary)
        self.registry.save()
        self._log_summary(summary)
        return summary

    async def _post_new_papers(self, summary: RunSummary):
        try:
            papers = await self.arxiv.search_recent()
        except Exception as e:
            self.logger.exception(f"Failed to fetch papers: {e}")
            return

        send_failures = 0
        for paper in papers:
            if paper.id in self.announced_new:
                continue
            self.announced_new.add(paper.id)

            # The fetch window overlaps earlier runs, so most papers here were
            # already posted; check that before spending any Crossref calls.
            if self.registry.get(paper.id).posted:
                self.logger.info(f"Paper {paper.id} was already posted in a previous run, skipping.")
                continue

            # Enrich before posting so the initial announcement already
            # includes DOI/journal data when arXiv or Crossref has it.
            paper = await self.publications.enrich(paper)
            self.logger.info(f"Processing paper: '{paper.title}' ({paper.id})")
            delivered = await self._announce(paper, "new")
            if delivered:
                summary.papers_posted += 1
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
            self.logger.warning(f"{send_failures} message(s) failed to send; they will be retried next run.")
        elif not self.settings.no_send:
            self.arxiv.commit_coverage()

    async def _hydrate_missing_metadata(self, summary: RunSummary):
        """Backfills title/author metadata for legacy or sparse registry records."""
        candidate_ids = self.registry.missing_metadata()
        if not candidate_ids:
            self.logger.info("No tracked paper records need metadata hydration.")
            return

        self.logger.info(f"Hydrating metadata for {len(candidate_ids)} incomplete tracked paper records.")
        try:
            refresh = await self.arxiv.fetch_by_ids(candidate_ids)
        except Exception as e:
            self.logger.exception(f"Failed to hydrate tracked paper metadata: {e}")
            summary.degraded = True
            return

        for paper in refresh.papers:
            self.registry.record_paper(paper)
            summary.records_hydrated += 1

        if refresh.degraded:
            summary.degraded = True
            self.logger.warning(
                "Metadata hydration was partial: refreshed %d/%d tracked papers; "
                "%d were deferred to a future run.",
                len(refresh.papers),
                len(candidate_ids),
                len(refresh.failed_ids),
            )

    async def _post_publication_updates(self, summary: RunSummary):
        """Announces tracked preprints that now have journal publication metadata."""
        tracked = self.registry.awaiting_publication()
        if not tracked:
            self.logger.info("No tracked unpublished papers require publication checks.")
            return

        self.logger.info(f"Checking publication status for {len(tracked)} tracked papers.")
        try:
            refresh = await self.publications.find_updates(
                list(tracked),
                tracked_metadata={
                    paper_id: TrackedPaperMetadata(
                        title=record.title,
                        authors=list(record.authors),
                        doi=record.doi,
                        journal_ref=record.journal_ref,
                    )
                    for paper_id, record in tracked.items()
                },
            )
        except Exception as e:
            self.logger.exception(f"Failed to refresh publication metadata: {e}")
            summary.degraded = True
            return

        if refresh.degraded:
            summary.degraded = True
            self.logger.warning(
                "Publication metadata refresh was partial: checked %d/%d tracked papers; "
                "%d were deferred to a future run.",
                len(refresh.checked_ids),
                len(tracked),
                len(refresh.failed_ids),
            )

        for paper in refresh.updates:
            if paper.id in self.announced_published:
                continue
            self.announced_published.add(paper.id)
            # The flag is flipped only after a successful send, so each
            # publication is announced exactly once.
            if await self._announce(paper, "published"):
                summary.publication_updates += 1
                self.registry.record_paper(paper, published=True)
                self.registry.save()

    async def _announce(self, paper: Paper, event_type: str) -> Optional[bool]:
        """
        Formats and sends one message. Returns True if it was delivered, False
        if sending failed, and None if it was skipped (unformattable, or a
        --nosend dry run).
        """
        message = format_paper_message(paper, self.settings, event_type=event_type)
        if not message:
            self.logger.warning(f"Skipping {event_type} message for '{paper.title}': formatting failed (likely too long).")
            return None

        if self.settings.no_send:
            self.logger.info(f"[NO_SEND] Would post {event_type} message for paper: {paper.title}")
            self.logger.debug(f"Message content:\n{message}")
            return None

        self.logger.info(f"Sending {event_type} message for paper: {paper.title}")
        return await self.send(message)

    def _log_summary(self, summary: RunSummary):
        if summary.papers_posted == 0 and summary.publication_updates == 0:
            if summary.degraded:
                self.logger.warning(
                    "No new papers were posted, and publication checks were incomplete because "
                    "arXiv metadata refresh was throttled or temporarily unavailable."
                )
            else:
                self.logger.info("No new papers or publication updates found matching criteria.")
            return

        self.logger.info(
            f"Finished processing. Posted {summary.papers_posted} new paper notifications and "
            f"{summary.publication_updates} publication updates; hydrated metadata for "
            f"{summary.records_hydrated} tracked records."
        )
        if summary.degraded:
            self.logger.warning(
                "Publication checks completed with gaps because some tracked papers "
                "could not be refreshed from arXiv this run."
            )
