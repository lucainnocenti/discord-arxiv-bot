"""Formatting helpers that keep paper announcements within Discord limits."""

import logging
from typing import List, Optional, Protocol

from models import Paper
from tracked_authors import TrackedAuthorList

MAX_DISCORD_MSG_LEN = 2000
# Below this much room for the abstract, the author list becomes "First et al.".
MIN_ABSTRACT_ROOM = 600
TRUNCATION_SUFFIX = "... [truncated]"

logger = logging.getLogger(__name__)


class Shortener(Protocol):
    async def shorten(self, abstract: str, max_length: int) -> Optional[str]: ...


def discord_length(text: str) -> int:
    """Length as Discord's limit counts it, erring on the long side.

    Counted in UTF-16 code units: the same as len() for ordinary text, but a
    character outside the Basic Multilingual Plane (some math symbols, emoji)
    counts as two, so a message that passes here is never over the limit.
    """
    return len(text.encode("utf-16-le")) // 2


async def format_new_paper(
    paper: Paper,
    tracked: TrackedAuthorList,
    shortener: Optional[Shortener] = None,
) -> Optional[str]:
    """Formats a new-paper announcement; None if it can't fit in one message.

    The abstract is posted unchanged whenever it fits. Otherwise `shortener`
    condenses it to the room left; without one, or if it fails, the abstract
    is cut at a word boundary.
    """
    mentions = _mention_tracked_authors(paper, tracked)
    journal_line = f"**Journal Reference:** {paper.journal_ref}\n" if paper.journal_ref else ""
    doi_line = f"**DOI:** {paper.doi}\n" if paper.doi else ""

    def render(authors: str, abstract: str) -> str:
        return (
            f"**New paper by {mentions}**\n"
            f"**Title:** {paper.title}\n"
            f"**Authors:** {authors}\n"
            f"**Submitted:** {paper.submitted:%Y-%m-%d}\n"
            f"**Abstract:** {abstract}\n"
            f"{journal_line}"
            f"{doi_line}"
            f"<{paper.link}>"
        )

    abstract = paper.summary
    first_author = paper.authors[0] if paper.authors else "Unknown"
    author_options = [", ".join(paper.authors), f"{first_author} et al."]
    # The abstract is the only variable part, so the room for it is exact.
    rooms = [MAX_DISCORD_MSG_LEN - discord_length(render(authors, "")) for authors in author_options]

    # Keep the full author list if the abstract fits with it, or if it still
    # leaves a reasonable amount of room for a shortened abstract.
    choice = next((i for i, room in enumerate(rooms) if discord_length(abstract) <= room), None)
    if choice is None:
        choice = next((i for i, room in enumerate(rooms) if room >= MIN_ABSTRACT_ROOM), len(rooms) - 1)
    authors, room = author_options[choice], rooms[choice]

    if room <= discord_length(TRUNCATION_SUFFIX):
        logger.error(f"Message for paper '{paper.title}' is too long even without an abstract and with 'et al.'. Skipping.")
        return None

    if discord_length(abstract) > room:
        logger.info(f"Abstract of '{paper.title}' is {discord_length(abstract)} characters; {room} fit in the message.")
        shortened = await shortener.shorten(abstract, room) if shortener else None
        abstract = shortened if shortened is not None else _truncate(abstract, room)

    message = render(authors, abstract)
    if discord_length(message) > MAX_DISCORD_MSG_LEN:
        # Can't happen given the room computed above; never send an oversized message.
        logger.error(f"Message for paper '{paper.title}' came out at {discord_length(message)} characters. Skipping.")
        return None
    return message


def _truncate(abstract: str, room: int) -> str:
    """Cuts the abstract at a word boundary so it plus the suffix fit in `room`."""
    limit = room - discord_length(TRUNCATION_SUFFIX)
    cut = abstract[:limit]
    while discord_length(cut) > limit:  # only with characters that count double
        cut = cut[:-1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + TRUNCATION_SUFFIX


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
    if discord_length(message) > MAX_DISCORD_MSG_LEN:
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
