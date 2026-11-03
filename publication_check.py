"""Decides whether tracked preprints have been published in a journal.

arXiv metadata is the primary signal: a DOI or journal reference appearing
there means the authors linked the preprint to its journal version. Crossref
is a conservative fallback for papers whose arXiv entry was never updated.
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

from arxiv_api import ArxivApi
from crossref import CrossrefClient, journal_ref_needs_enrichment, merge_crossref_item
from models import Paper
from registry import PaperRecord


class PublicationChecker:
    def __init__(self, api: ArxivApi, crossref: CrossrefClient):
        self.api = api
        self.crossref = crossref

    async def find_updates(self, tracked: Dict[str, PaperRecord]) -> List[Paper]:
        """Refreshes tracked papers from arXiv and returns the ones that now look published."""
        papers, failed_ids = await self.api.fetch_by_ids(list(tracked))
        # Papers arXiv could not refresh are still checked on Crossref, from
        # the metadata stored when they were posted.
        papers += [_paper_from_record(paper_id, tracked[paper_id]) for paper_id in failed_ids]

        updates = []
        for paper in papers:
            update = await self.check(paper)
            if update:
                updates.append(update)
        return updates

    async def check(self, paper: Paper) -> Optional[Paper]:
        """The journal version of `paper`, or None if it still looks unpublished."""
        if paper.doi or paper.journal_ref:
            return await self.enrich(paper)
        # Crossref is only consulted for papers that still look unpublished
        # on arXiv, which helps keep false positives low.
        return await asyncio.to_thread(self.crossref.find_publication, paper)

    async def enrich(self, paper: Paper) -> Paper:
        """Upgrades a paper's DOI and journal reference from Crossref when it can help."""
        if not paper.doi and not paper.journal_ref:
            return paper

        if paper.doi:
            # A DOI is the strongest key Crossref offers, so try that before any
            # fuzzier title/author matching.
            item = await asyncio.to_thread(self.crossref.fetch_work_by_doi, paper.doi)
            enriched = merge_crossref_item(paper, item) if item else None
            if enriched:
                return enriched

        if journal_ref_needs_enrichment(paper.journal_ref):
            # Fall back to a conservative search only when the existing journal
            # reference looks too sparse to be useful in Discord messages.
            match = await asyncio.to_thread(self.crossref.find_publication, paper)
            if match:
                return match

        return paper


def _paper_from_record(paper_id: str, record: PaperRecord) -> Paper:
    """Builds a Paper from a registry record, for when arXiv can't be reached."""
    return Paper(
        id=paper_id,
        title=record.title or "",
        authors=list(record.authors),
        submitted=datetime.now(timezone.utc),  # unknown, and not needed for a publication update
        summary="",
        link=f"https://arxiv.org/abs/{paper_id}",
        doi=record.doi,
        journal_ref=record.journal_ref,
    )
