"""One-off utility to mark legacy publication updates as already notified.

This is intended for the migration period after publication-update tracking was
added to the bot. It inspects already-posted arXiv papers that are still marked
as unpublished and, if they now expose a DOI, records them as published in the
registry without sending any Discord messages.
"""

import argparse
import asyncio
import logging

from arxiv_fetcher import ArxivFetcher, TrackedPaperMetadata
from bot import setup_logging
from settings import load_settings
from state_manager import StateManager


async def backfill_published_registry():
    """Backfills publication state for already-posted papers."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of tracked unpublished papers to inspect.",
    )
    args, _ = parser.parse_known_args()

    settings = load_settings()
    setup_logging(settings.log_path)

    state_manager = StateManager(settings)
    fetcher = ArxivFetcher(settings)
    registry = state_manager.get_paper_registry()

    # Only inspect papers that were already announced in Discord but have not
    # yet been marked as published in the local registry.
    candidate_ids = [
        paper_id
        for paper_id, record in registry.items()
        if record.posted and not record.published
    ]
    if args.limit is not None:
        candidate_ids = candidate_ids[:args.limit]

    logging.info(f"Inspecting {len(candidate_ids)} tracked unpublished papers for DOI backfill.")
    if not candidate_ids:
        print("No tracked unpublished papers found.")
        return

    publication_updates = await fetcher.fetch_publication_updates(
        candidate_ids,
        tracked_metadata={
            paper_id: TrackedPaperMetadata(
                title=record.title,
                authors=list(record.authors),
                doi=record.doi,
                journal_ref=record.journal_ref,
            )
            for paper_id, record in registry.items()
            if paper_id in candidate_ids
        },
    )

    updated_count = 0
    skipped_without_doi = 0
    for paper in publication_updates:
        # This backfill is intentionally conservative: only mark papers as
        # already-notified when we have a DOI, so future runs can still notify
        # on weaker metadata changes if needed.
        if not paper.doi:
            skipped_without_doi += 1
            continue

        state_manager.upsert_paper_record(
            paper.id,
            posted=True,
            published=True,
            doi=paper.doi,
            journal_ref=paper.journal_ref,
            title=paper.title,
            authors=paper.authors,
        )
        updated_count += 1

    print(
        f"Backfill complete. Marked {updated_count} papers as already-notified publications; "
        f"skipped {skipped_without_doi} publication candidates without a DOI."
    )


if __name__ == "__main__":
    asyncio.run(backfill_published_registry())
