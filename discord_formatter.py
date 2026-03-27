# discord_formatter.py
import logging
from typing import Dict, List, Optional

from arxiv_fetcher import Paper
from settings import AppSettings

MAX_SUMMARY_LEN = 1400
MAX_DISCORD_MSG_LEN = 2000


def format_paper_message(paper: Paper, settings: AppSettings, event_type: str = "new") -> Optional[str]:
    """Formats paper details into a Discord message string."""
    target_authors_str = _build_target_authors_string(
        paper.authors,
        settings.target_authors,
        settings.author_discord_ids,
    )

    # Publication updates use a different, shorter template because the
    # interesting part is the journal/DOI metadata rather than the abstract.
    if event_type == "published":
        return _format_publication_update(paper, target_authors_str)

    summary = paper.summary
    if len(summary) > MAX_SUMMARY_LEN:
        summary = summary[:MAX_SUMMARY_LEN] + "... [truncated]"

    published_str = paper.published.strftime('%Y-%m-%d')
    journal_line = f"**Journal Reference:** {paper.journal_ref}\n" if paper.journal_ref else ""
    doi_line = f"**DOI:** {paper.doi}\n" if paper.doi else ""

    message_template = (
        "**New paper by {target_authors}**\n"
        "**Title:** {title}\n"
        "**Authors:** {authors_str}\n"
        "**Announced:** {published_date}\n"
        "**Abstract:** {summary}\n"
        "{journal_ref_line}"
        "{doi_line}"
        "<{link}>"
    )

    full_authors_str = ', '.join(paper.authors)
    message = message_template.format(
        target_authors=target_authors_str,
        title=paper.title,
        authors_str=full_authors_str,
        published_date=published_str,
        summary=summary,
        journal_ref_line=journal_line,
        doi_line=doi_line,
        link=paper.link,
    )

    if len(message) > MAX_DISCORD_MSG_LEN:
        logging.info(f"Message for '{paper.title}' too long with full authors, trying 'et al.'")
        first_author = paper.authors[0] if paper.authors else "Unknown"
        message = message_template.format(
            target_authors=target_authors_str,
            title=paper.title,
            authors_str=f"{first_author} et al.",
            published_date=published_str,
            summary=summary,
            journal_ref_line=journal_line,
            doi_line=doi_line,
            link=paper.link,
        )

        if len(message) > MAX_DISCORD_MSG_LEN:
            logging.error(
                f"Message for paper '{paper.title}' is still too long ({len(message)} chars) even with 'et al.'. Skipping."
            )
            return None

    return message


def _format_publication_update(paper: Paper, target_authors_str: str) -> Optional[str]:
    """Formats a short message announcing that a tracked preprint has been published."""
    details: List[str] = []
    if paper.journal_ref:
        details.append(f"**New journal reference:** {paper.journal_ref}")
    if paper.doi:
        details.append(f"**DOI:** https://doi.org/{paper.doi}")

    if not details:
        logging.warning(f"Publication update requested for '{paper.title}' without journal metadata.")
        return None

    message = (
        f"📄 **Update to paper by {target_authors_str}:**\n"
        f"The arXiv paper <{paper.link}> was published! Cheers! 🥂🍾\n"
        + "\n".join(details)
    )
    if len(message) > MAX_DISCORD_MSG_LEN:
        logging.warning(f"Update message for '{paper.title}' too long even after formatting.")
        return None
    return message


def _build_target_authors_string(
    paper_authors: List[str],
    target_authors: List[str],
    author_discord_ids: Dict[str, int],
) -> str:
    """Constructs a tagged string of target authors found in the paper."""
    # Match against the configured author list first, then replace matched names
    # with Discord mentions when an ID is available.
    target_in_paper: List[str] = []
    paper_authors_lower = {p.lower() for p in paper_authors}
    for target in target_authors:
        if target.lower() in paper_authors_lower:
            target_in_paper.append(target)

    if not target_in_paper:
        return "tracked authors"

    if paper_authors:
        first_author_lower = paper_authors[0].lower()
        for i, target in enumerate(target_in_paper):
            if target.lower() == first_author_lower:
                target_in_paper.insert(0, target_in_paper.pop(i))
                break

    tagged_authors: List[str] = []
    for author in target_in_paper:
        discord_id = author_discord_ids.get(author)
        if discord_id:
            tagged_authors.append(f"<@{discord_id}>")
        else:
            tagged_authors.append(author)

    if len(tagged_authors) == 1:
        return tagged_authors[0]
    if len(tagged_authors) == 2:
        return f"{tagged_authors[0]} and {tagged_authors[1]}"
    return ", ".join(tagged_authors[:-1]) + ", and " + tagged_authors[-1]
