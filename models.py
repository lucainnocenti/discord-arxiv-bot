"""The shared Paper type and arXiv ID / text normalization."""

import re
from datetime import datetime
from typing import Any, List, NamedTuple, Optional

ARXIV_ID_PATTERN = re.compile(r'(\d{4}\.\d{4,5})(v\d+)?')


class Paper(NamedTuple):
    """Represents a normalized arXiv paper with key details."""
    id: str              # Canonical arXiv identifier (for example, '2503.16215')
    title: str           # Paper title
    authors: List[str]   # List of author names
    published: datetime  # First-version submission datetime (timezone-aware)
    summary: str         # Paper abstract/summary
    link: str            # Link to the abstract page
    pdf_link: str        # Direct link to the PDF
    doi: Optional[str]   # DOI, if available
    journal_ref: Optional[str] # Journal reference, if available (e.g., "Phys. Rev. Lett. ...")


def canonicalize_paper_id(raw_id: str) -> str:
    """Converts arXiv URLs and OAI identifiers to a stable arXiv ID without the version suffix."""
    normalized = raw_id.strip()
    match = ARXIV_ID_PATTERN.search(normalized)
    if match:
        return match.group(1)

    if '/abs/' in normalized:
        normalized = normalized.split('/abs/', 1)[-1]
    elif normalized.lower().startswith('oai:arxiv.org:'):
        normalized = normalized.split(':', 2)[-1]

    return normalized.removeprefix('abs/').split('v', 1)[0]


def clean_optional_text(value: Any) -> Optional[str]:
    """Strips text fields and normalizes blank strings to None."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
