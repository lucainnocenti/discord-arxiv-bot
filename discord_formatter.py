"""Formatting helpers that keep paper announcements within Discord limits."""

import logging
from typing import List, Optional

from models import Paper
from tracked_authors import TrackedAuthorList

MAX_SUMMARY_LEN = 1400
MAX_DISCORD_MSG_LEN = 2000

logger = logging.getLogger(__name__)


def format_new_paper(paper: Paper, tracked: TrackedAuthorList) -> Optional[str]:
    """Formats a new-paper announcement; None if it can't fit in one message."""
    mentions = _mention_tracked_authors(paper, tracked)
    summary = paper.summary
    if len(summary) > MAX_SUMMARY_LEN:
        # Trim early so the length fallback below only has to deal with
        # title/author growth, not arbitrarily long abstracts.
        summary = summary[:MAX_SUMMARY_LEN] + "... [truncated]"
    journal_line = f"**Journal Reference:** {paper.journal_ref}\n" if paper.journal_ref else ""
    doi_line = f"**DOI:** {paper.doi}\n" if paper.doi else ""

    # Preserve the full abstract when possible by shortening the author list
    # before giving up on the message entirely.
    first_author = paper.authors[0] if paper.authors else "Unknown"
    for authors in (", ".join(paper.authors), f"{first_author} et al."):
        message = (
            f"**New paper by {mentions}**\n"
            f"**Title:** {paper.title}\n"
            f"**Authors:** {authors}\n"
            f"**Submitted:** {paper.submitted:%Y-%m-%d}\n"
            f"**Abstract:** {summary}\n"
            f"{journal_line}"
            f"{doi_line}"
            f"<{paper.link}>"
        )
        if len(message) <= MAX_DISCORD_MSG_LEN:
            return message

    logger.error(f"Message for paper '{paper.title}' is too long ({len(message)} chars) even with 'et al.'. Skipping.")
    return None


def format_publication(paper: Paper, tracked: TrackedAuthorList) -> Optional[str]:
    """Formats a short message announcing that a tracked preprint has been published."""
    details: List[str] = []
    if paper.journal_ref:
        details.append(f"**New journal reference:** {paper.journal_ref}")
    if paper.doi:
        details.append(f"**DOI:** https://doi.org/{paper.doi}")

    if not details:
        logger.warning(f"Publication update requested for '{paper.title}' without journal metadata.")
        return None

    message = (
        f"📄 **Update to paper by {_mention_tracked_authors(paper, tracked)}:**\n"
        f"The arXiv paper <{paper.link}> was published! Cheers! 🥂🍾\n"
        + "\n".join(details)
    )
    if len(message) > MAX_DISCORD_MSG_LEN:
        logger.warning(f"Update message for '{paper.title}' too long even after formatting.")
        return None
    return message


def _mention_tracked_authors(paper: Paper, tracked: TrackedAuthorList) -> str:
    """Lists the paper's tracked authors, as Discord mentions where their ID is known."""
    names = [
        f"<@{author.discord_id}>" if author.discord_id else author.name
        for author in tracked.found_in(paper.authors)
    ]
    if not names:
        return "tracked authors"
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]
