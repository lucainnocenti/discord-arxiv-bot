"""Crossref lookups: finding a journal version of a preprint and formatting
its citation. Requests are synchronous and paced; callers run them in a
thread pool."""

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from typing import Any, Dict, Optional

from models import Paper, clean_optional_text

CROSSREF_API_URL = "https://api.crossref.org/works"
# Crossref asks clients to stay well below its rate limit; one request per
# second keeps the bot comfortably polite.
CROSSREF_MIN_INTERVAL_SECONDS = 1.0
CROSSREF_MAX_RETRY_AFTER_SECONDS = 60
# Publication matches are much noisier than new-paper detection, so the title
# check errs on the side of missing a match rather than claiming a wrong DOI.
MIN_TITLE_SIMILARITY = 0.93
TITLE_NORMALIZATION_PATTERN = re.compile(r'[^a-z0-9]+')


class CrossrefClient:
    def __init__(self, mailto: Optional[str] = None):
        self.mailto = mailto
        self.logger = logging.getLogger(self.__class__.__name__)
        # Lookups run in executor threads, so pacing needs a lock.
        self._lock = threading.Lock()
        self._last_request = 0.0

    def find_publication(self, paper: Paper) -> Optional[Paper]:
        """Searches Crossref for the journal version of `paper` by title and first author."""
        if not paper.title or not paper.authors:
            return None

        # Keep the query narrow: same title, same first author, journal articles
        # only, and just the fields needed for the publication-update decision.
        params = {
            "rows": "5",
            "query.title": paper.title,
            "query.author": paper.authors[0],
            "filter": "type:journal-article",
            "select": "DOI,title,author,container-title,type,volume,issue,page,article-number,published-print,published-online,issued",
        }
        if self.mailto:
            params["mailto"] = self.mailto

        try:
            payload = self._get_json(f"{CROSSREF_API_URL}?{urllib.parse.urlencode(params)}")
        except Exception as e:
            self.logger.warning(f"Crossref lookup failed for {paper.id}: {e}")
            return None

        items = payload.get("message", {}).get("items", [])
        if not isinstance(items, list):
            return None

        for item in items:
            # Stop at the first high-confidence match; this path is meant to be
            # conservative, not exhaustive.
            candidate = self.publication_from_item(paper, item)
            if candidate:
                self.logger.info(f"Crossref matched tracked paper {paper.id} to DOI {candidate.doi}.")
                return candidate
        return None

    def fetch_work_by_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        """Fetches a specific Crossref work by DOI."""
        try:
            payload = self._get_json(f"{CROSSREF_API_URL}/{urllib.parse.quote(doi)}")
        except Exception as e:
            self.logger.warning(f"Crossref DOI lookup failed for {doi}: {e}")
            return None
        item = payload.get("message")
        return item if isinstance(item, dict) else None

    def publication_from_item(self, paper: Paper, item: Dict[str, Any]) -> Optional[Paper]:
        """Validates a Crossref search hit and converts it into a publication update."""
        if item.get("type") != "journal-article":
            return None

        doi = item.get("DOI")
        titles = item.get("title", [])
        if not isinstance(doi, str) or not titles:
            return None

        crossref_title = titles[0] if isinstance(titles[0], str) else ""
        if not crossref_title or not is_probable_match(paper, crossref_title, item.get("author", [])):
            return None

        return merge_crossref_item(paper, item)

    def _get_json(self, url: str) -> Any:
        """
        Performs one Crossref request, paced to at most one per second.

        On HTTP 429 it waits as long as Crossref asks (capped) and retries once;
        any other failure is raised to the caller.
        """
        request = urllib.request.Request(url, headers={"User-Agent": self._user_agent()})
        for attempt in range(2):
            with self._lock:
                wait = self._last_request + CROSSREF_MIN_INTERVAL_SECONDS - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last_request = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    return json.load(response)
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 1:
                    raise
                retry_after = _retry_after_seconds(e)
                self.logger.info(f"Crossref rate limit hit; waiting {retry_after:.0f}s before retrying.")
                time.sleep(retry_after)

    def _user_agent(self) -> str:
        """Builds a polite Crossref User-Agent string."""
        if self.mailto:
            return f"arxiv-discord-bot/1.0 (mailto:{self.mailto})"
        return "arxiv-discord-bot/1.0"


def is_probable_match(paper: Paper, crossref_title: str, crossref_authors: Any) -> bool:
    """Uses a conservative title + first-author check to avoid false-positive DOI matches."""
    paper_title = _normalize_title(paper.title)
    candidate_title = _normalize_title(crossref_title)
    if not paper_title or not candidate_title:
        return False

    title_similarity = SequenceMatcher(None, paper_title, candidate_title).ratio()
    if title_similarity < MIN_TITLE_SIMILARITY and paper_title != candidate_title:
        return False

    first_author_family = _family_name(paper.authors[0])
    if not first_author_family:
        return False

    candidate_families = set()
    if isinstance(crossref_authors, list):
        for author in crossref_authors:
            if not isinstance(author, dict):
                continue
            family_name = author.get("family")
            full_name = author.get("name")
            if isinstance(family_name, str):
                candidate_families.add(_family_name(family_name))
            elif isinstance(full_name, str):
                candidate_families.add(_family_name(full_name))

    return first_author_family in candidate_families


def merge_crossref_item(paper: Paper, item: Dict[str, Any]) -> Optional[Paper]:
    """Returns `paper` with the best DOI and journal reference from a Crossref work."""
    doi = clean_optional_text(item.get("DOI")) or paper.doi
    journal_ref = choose_better_journal_ref(paper.journal_ref, format_journal_ref(item))
    if not doi and not journal_ref:
        return None
    return paper._replace(doi=doi, journal_ref=journal_ref)


def format_journal_ref(item: Dict[str, Any]) -> Optional[str]:
    """Builds a citation like 'Physical Review A 113.3 (2026): 032201' from a Crossref work."""
    container_titles = item.get("container-title", [])
    journal = None
    if isinstance(container_titles, list) and container_titles:
        journal = clean_optional_text(container_titles[0])
    if not journal:
        return None

    volume = clean_optional_text(item.get("volume"))
    issue = clean_optional_text(item.get("issue"))
    pages = clean_optional_text(item.get("page")) or clean_optional_text(item.get("article-number"))
    year = _extract_year(item)

    citation = journal
    if volume:
        citation += f" {volume}"
        if issue:
            citation += f".{issue}"
    if year:
        citation += f" ({year})"
    if pages:
        citation += f": {pages}"
    return citation


def choose_better_journal_ref(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    """Prefers the more informative journal reference."""
    current_clean = clean_optional_text(current)
    candidate_clean = clean_optional_text(candidate)
    if not candidate_clean:
        return current_clean
    if not current_clean:
        return candidate_clean
    if _journal_ref_score(candidate_clean) > _journal_ref_score(current_clean):
        return candidate_clean
    return current_clean


def journal_ref_needs_enrichment(journal_ref: Optional[str]) -> bool:
    """Returns True when the journal reference is missing or looks sparse."""
    cleaned = clean_optional_text(journal_ref)
    return not cleaned or _journal_ref_score(cleaned) < 2


def _journal_ref_score(journal_ref: str) -> int:
    """Scores how citation-like a journal reference is (year, volume, pages)."""
    score = 0
    if re.search(r'\b\d{4}\b', journal_ref):
        score += 1
    if re.search(r'\b\d+(?:\.\d+)?\b', journal_ref):
        score += 1
    if ":" in journal_ref or re.search(r'\b[A-Z]?\d{4,}\b', journal_ref):
        score += 1
    return score


def _extract_year(item: Dict[str, Any]) -> Optional[str]:
    """Returns the most useful year present in a Crossref record."""
    for field in ("published-print", "published-online", "issued"):
        date_part = item.get(field)
        if not isinstance(date_part, dict):
            continue
        parts = date_part.get("date-parts")
        if not isinstance(parts, list) or not parts:
            continue
        first = parts[0]
        if not isinstance(first, list) or not first:
            continue
        year = first[0]
        if isinstance(year, int):
            return str(year)
        if isinstance(year, str) and year.strip():
            return year.strip()
    return None


def _retry_after_seconds(error: urllib.error.HTTPError) -> float:
    try:
        seconds = float(error.headers.get("Retry-After", 5))
    except (TypeError, ValueError, AttributeError):
        seconds = 5.0
    return min(max(seconds, 1.0), CROSSREF_MAX_RETRY_AFTER_SECONDS)


def _normalize_title(title: str) -> str:
    """Lowercases and strips punctuation so near-identical titles compare reliably."""
    return TITLE_NORMALIZATION_PATTERN.sub('', title.casefold())


def _family_name(author_name: str) -> str:
    """Extracts a loose last-name token for conservative author matching."""
    tokens = TITLE_NORMALIZATION_PATTERN.sub(' ', author_name.casefold()).split()
    return tokens[-1] if tokens else ""
