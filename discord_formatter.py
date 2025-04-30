# discord_formatter.py
import logging
from typing import List, Dict, Optional
from datetime import timedelta

from arxiv_fetcher import Paper # Use the Paper structure
from settings import AppSettings, RSS_SOURCE

MAX_SUMMARY_LEN = 1400
MAX_DISCORD_MSG_LEN = 2000 # Discord's limit

def format_paper_message(paper: Paper, settings: AppSettings) -> Optional[str]:
    """Formats paper details into a Discord message string."""

    target_authors_str = _build_target_authors_string(
        paper.authors,
        settings.target_authors,
        settings.author_discord_ids
    )

    # Truncate summary if needed
    summary = paper.summary
    if len(summary) > MAX_SUMMARY_LEN:
        summary = summary[:MAX_SUMMARY_LEN] + "... [truncated]"

    # Format published date (adjusting for display - arXiv submission day)
    # API `published` is usually submission time. RSS `published` is announcement time (often midnight ET).
    published_str = paper.published.strftime('%Y-%m-%d')

    journal_line = f"**Journal Reference:** {paper.journal_ref}\n" if paper.journal_ref else ""

    # Special message for RSS updates that got published
    if settings.source == RSS_SOURCE and paper.announce_type == 'replace' and paper.journal_ref:
        message = (
            f"📄 **Update to paper by {target_authors_str}**:\n"
            f"The arXiv paper <{paper.link}> was published! Cheers! 🥂🍾\n"
            f"**New journal reference:** {paper.journal_ref}"
        )
        # Update messages are usually short, but check length just in case
        if len(message) > MAX_DISCORD_MSG_LEN:
             logging.warning(f"Update message for '{paper.title}' too long even after formatting.")
             # Decide how to handle: truncate further, skip, send partial? Skipping is simplest.
             return None
        return message

    # Standard message format
    message_template = (
        "📄 **New paper by {target_authors}**:\n"
        "**Title:** {title}\n"
        "**Authors:** {authors_str}\n"
        "**Announced:** {published_date}\n"
        "**Abstract:** {summary}\n"
        "{journal_ref_line}"
        "🔗 <{link}>" # Add PDF link too
    )

    # Attempt full author list first
    full_authors_str = ', '.join(paper.authors)
    message = message_template.format(
        target_authors=target_authors_str,
        title=paper.title,
        authors_str=full_authors_str,
        published_date=published_str,
        summary=summary,
        journal_ref_line=journal_line,
        link=paper.link
    )

    # If too long, try "et al."
    if len(message) > MAX_DISCORD_MSG_LEN:
        logging.info(f"Message for '{paper.title}' too long with full authors, trying 'et al.'")
        first_author = paper.authors[0] if paper.authors else "Unknown"
        etal_authors_str = f"{first_author} et al."
        message = message_template.format(
            target_authors=target_authors_str,
            title=paper.title,
            authors_str=etal_authors_str,
            published_date=published_str,
            summary=summary,
            journal_ref_line=journal_line,
            link=paper.link,
            pdf_link=paper.pdf_link
        )

        # If *still* too long, log error and skip
        if len(message) > MAX_DISCORD_MSG_LEN:
            logging.error(f"Message for paper '{paper.title}' is still too long ({len(message)} chars) even with 'et al.'. Skipping.")
            return None # Indicate failure to format

    return message


def _build_target_authors_string(
    paper_authors: List[str],
    target_authors: List[str],
    author_discord_ids: Dict[str, int]
) -> str:
    """Constructs a tagged string of target authors found in the paper."""
    # Find matches (case-insensitive)
    target_in_paper: List[str] = []
    paper_authors_lower = {p.lower() for p in paper_authors}
    for target in target_authors:
        if target.lower() in paper_authors_lower:
            target_in_paper.append(target) # Keep original casing for lookup

    if not target_in_paper:
        return "tracked authors" # Or "Unknown Target Author"

    # Optional: Reorder to put the first author first if they are a target
    if paper_authors:
        first_author_lower = paper_authors[0].lower()
        for i, target in enumerate(target_in_paper):
            if target.lower() == first_author_lower:
                # Move target to the front
                target_in_paper.insert(0, target_in_paper.pop(i))
                break

    # Build tagged list
    tagged_authors: List[str] = []
    for author in target_in_paper:
        discord_id = author_discord_ids.get(author) # Case-sensitive lookup matches config
        if discord_id:
            tagged_authors.append(f"<@{discord_id}>")
        else:
            tagged_authors.append(author) # Fallback to name if no ID

    # Format the final string
    if len(tagged_authors) == 0: # Should not happen if target_in_paper is not empty
         return "tracked authors"
    elif len(tagged_authors) == 1:
        return tagged_authors[0]
    elif len(tagged_authors) == 2:
        return f"{tagged_authors[0]} and {tagged_authors[1]}"
    else:
        # Oxford comma style: "A, B, and C"
        return ", ".join(tagged_authors[:-1]) + ", and " + tagged_authors[-1]