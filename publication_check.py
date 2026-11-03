"""Decides whether tracked preprints have been published in a journal.

arXiv metadata is the primary signal: a DOI or journal reference appearing
there means the authors linked the preprint to its journal version. Crossref
is a conservative fallback for papers whose arXiv entry was never updated.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from arxiv_source import ArxivSource
from crossref import CrossrefClient, journal_ref_needs_enrichment, merge_crossref_item
from models import (
    Paper,
    PublicationRefreshResult,
    TrackedPaperMetadata,
    canonicalize_paper_id,
    clean_optional_text,
)


class PublicationChecker:
    def __init__(self, arxiv_source: ArxivSource, crossref: CrossrefClient):
        self.arxiv = arxiv_source
        self.crossref = crossref
        self.logger = logging.getLogger(self.__class__.__name__)
        self.crossref_requests = 0

    async def find_updates(
        self,
        tracked_paper_ids: List[str],
        tracked_metadata: Optional[Dict[str, TrackedPaperMetadata]] = None,
    ) -> PublicationRefreshResult:
        """Refreshes tracked papers and returns the ones that now look published."""
        if not tracked_paper_ids:
            return PublicationRefreshResult(updates=[], checked_ids=[], failed_ids=[], degraded=False)

        metadata_by_id = _normalize_tracked_metadata(tracked_metadata or {})
        refresh = await self.arxiv.fetch_by_ids(tracked_paper_ids)
        updates: List[Paper] = []
        self.crossref_requests = 0
        fallback_attempts = 0
        fallback_skips = 0

        if refresh.failed_ids:
            self.logger.warning(
                "Publication metadata refresh completed with gaps: refreshed %d/%d tracked papers; "
                "%d could not be checked this run.",
                len(refresh.requested_ids) - len(refresh.failed_ids),
                len(refresh.requested_ids),
                len(refresh.failed_ids),
            )

        for paper in refresh.papers:
            # If arXiv itself already exposes publication metadata, prefer that
            # over any external inference.
            if paper.journal_ref or paper.doi:
                enriched = await self.enrich(paper)
                updates.append(enriched._replace(announce_type='published_metadata'))
                continue

            # Crossref is only consulted for papers that still look unpublished
            # on arXiv, which helps keep false positives low.
            match = await self._crossref_search(paper)
            if match:
                updates.append(match)

        for failed_id in refresh.failed_ids:
            stored_metadata = metadata_by_id.get(failed_id)
            if stored_metadata is None:
                fallback_skips += 1
                continue
            fallback_attempts += 1
            match = await self._crossref_fallback_for_failed_refresh(failed_id, stored_metadata)
            if match:
                updates.append(match)

        if refresh.failed_ids or self.crossref_requests:
            suffix = ""
            if fallback_skips:
                suffix = (
                    f"; {fallback_skips} failed arXiv refreshes lacked enough "
                    "stored metadata for Crossref fallback"
                )
            self.logger.info(
                "Publication update Crossref summary: %d requests issued; "
                "Crossref-only fallback attempted for %d failed arXiv refreshes%s.",
                self.crossref_requests,
                fallback_attempts,
                suffix,
            )
        return PublicationRefreshResult(
            updates=updates,
            checked_ids=[paper.id for paper in refresh.papers],
            failed_ids=refresh.failed_ids,
            degraded=refresh.degraded,
        )

    async def enrich(self, paper: Paper) -> Paper:
        """Normalizes journal metadata and upgrades sparse refs when Crossref can help."""
        # Normalize first so later checks do not have to care about blank strings
        # versus None when deciding whether metadata is actually present.
        paper = paper._replace(
            doi=clean_optional_text(paper.doi),
            journal_ref=clean_optional_text(paper.journal_ref),
        )
        if not paper.doi and not paper.journal_ref:
            return paper

        if paper.doi:
            # A DOI is the strongest key Crossref offers, so try that before any
            # fuzzier title/author matching.
            self.crossref_requests += 1
            item = await asyncio.to_thread(self.crossref.fetch_work_by_doi, paper.doi)
            if item:
                enriched = merge_crossref_item(paper, item, announce_type='crossref_doi')
                if enriched:
                    return enriched

        if journal_ref_needs_enrichment(paper.journal_ref):
            # Fall back to a conservative search only when the existing journal
            # reference looks too sparse to be useful in Discord messages.
            match = await self._crossref_search(paper)
            if match:
                return match

        return paper

    async def _crossref_search(self, paper: Paper) -> Optional[Paper]:
        self.crossref_requests += 1
        return await asyncio.to_thread(self.crossref.find_publication, paper)

    async def _crossref_fallback_for_failed_refresh(
        self,
        paper_id: str,
        metadata: TrackedPaperMetadata,
    ) -> Optional[Paper]:
        """Uses stored local metadata to query Crossref when the arXiv refresh failed."""
        fallback_paper = _paper_from_tracked_metadata(paper_id, metadata)
        if fallback_paper is None:
            return None

        if fallback_paper.doi or fallback_paper.journal_ref:
            enriched = await self.enrich(fallback_paper)
            if enriched.doi or enriched.journal_ref:
                return enriched._replace(announce_type='crossref_registry_fallback')
            return None

        return await self._crossref_search(fallback_paper)


def _normalize_tracked_metadata(
    tracked_metadata: Dict[str, TrackedPaperMetadata],
) -> Dict[str, TrackedPaperMetadata]:
    """Canonicalizes tracked-paper metadata keys so they match normalized arXiv IDs."""
    return {
        canonicalize_paper_id(paper_id): TrackedPaperMetadata(
            title=clean_optional_text(metadata.title),
            authors=[
                author.strip()
                for author in metadata.authors
                if isinstance(author, str) and author.strip()
            ],
            doi=clean_optional_text(metadata.doi),
            journal_ref=clean_optional_text(metadata.journal_ref),
        )
        for paper_id, metadata in tracked_metadata.items()
    }


def _paper_from_tracked_metadata(paper_id: str, metadata: TrackedPaperMetadata) -> Optional[Paper]:
    """Builds a synthetic Paper from locally stored metadata for Crossref fallback."""
    if not metadata.doi and (not metadata.title or not metadata.authors):
        return None

    return Paper(
        id=paper_id,
        title=metadata.title or f"arXiv:{paper_id}",
        authors=list(metadata.authors),
        published=datetime.now(timezone.utc),
        summary="",
        link=f"https://arxiv.org/abs/{paper_id}",
        pdf_link=f"https://arxiv.org/pdf/{paper_id}",
        doi=metadata.doi,
        journal_ref=metadata.journal_ref,
        announce_type='crossref_registry_seed',
    )
