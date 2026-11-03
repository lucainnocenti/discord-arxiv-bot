"""Decides how often each tracked paper is looked up on Crossref.

The arXiv refresh of tracked papers is cheap (batched, a few requests per run)
and stays daily. Crossref needs one request per paper, so lookups are spaced
out by paper age: journal publication usually takes months, and a preprint
that is still unpublished after a few years is unlikely to change.
"""

from datetime import date, timedelta
from typing import Optional

from arxiv_fetcher import ARXIV_ID_PATTERN

# (maximum paper age, minimum gap between Crossref lookups)
CROSSREF_SCHEDULE = (
    (timedelta(days=365), timedelta(days=7)),
    (timedelta(days=3 * 365), timedelta(days=30)),
)


def is_crossref_check_due(paper_id: str, last_checked: Optional[str], today: date) -> bool:
    """True when the paper should get a Crossref lookup on `today`."""
    age = paper_age(paper_id, today)
    if age is None:
        return False
    for max_age, interval in CROSSREF_SCHEDULE:
        if age <= max_age:
            if not last_checked:
                return True
            try:
                return today - date.fromisoformat(last_checked) >= interval
            except ValueError:
                return True
    # Older than the last schedule bracket: stop asking Crossref.
    return False


def paper_age(paper_id: str, today: date) -> Optional[timedelta]:
    """Approximates a paper's age from its new-style arXiv ID (YYMM.NNNNN)."""
    match = ARXIV_ID_PATTERN.search(paper_id)
    if not match:
        return None
    yymm = match.group(1).split(".")[0]
    submitted = date(2000 + int(yymm[:2]), int(yymm[2:]), 1)
    return today - submitted
