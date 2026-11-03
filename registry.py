"""The tracked-paper registry: which papers were announced, and which of
those were later announced as published.

The registry is loaded once per run and kept in memory as the single source
of truth; `save()` writes it back atomically as JSON lines (one record per
line, so the file stays easy to read and fix by hand).
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, Iterator, List, Optional, Tuple

from models import Paper, canonicalize_paper_id, clean_optional_text

logger = logging.getLogger("PaperRegistry")


@dataclass
class PaperRecord:
    # `posted` tracks whether the preprint announcement was sent.
    # `published` tracks whether the later journal-publication update was sent.
    posted: bool = False
    published: bool = False
    doi: Optional[str] = None
    journal_ref: Optional[str] = None
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)


class PaperRegistry:
    def __init__(self, path: str, records: Optional[Dict[str, PaperRecord]] = None, *, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        self._records: Dict[str, PaperRecord] = records or {}
        self._dirty = False

    @classmethod
    def load(cls, path: str, *, read_only: bool = False) -> "PaperRegistry":
        """
        Reads the registry file. A missing file means an empty registry; an
        unreadable file raises, because carrying on with an empty registry
        would re-announce every paper in the fetch window.
        """
        registry = cls(path, read_only=read_only)
        if not os.path.exists(path):
            logger.info(f"{path} not found. Starting with an empty registry.")
            return registry

        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                parsed = _parse_line(line)
                if parsed:
                    # update() merges duplicate lines, so repeated writes or
                    # manual edits do not lose information.
                    registry.update(parsed.pop("id"), **parsed)
        registry._dirty = False
        logger.info(f"Loaded {len(registry)} tracked paper records from {path}")
        return registry

    def get(self, paper_id: str) -> PaperRecord:
        return self._records.get(canonicalize_paper_id(paper_id), PaperRecord())

    def __contains__(self, paper_id: str) -> bool:
        return canonicalize_paper_id(paper_id) in self._records

    def __len__(self) -> int:
        return len(self._records)

    def items(self) -> Iterator[Tuple[str, PaperRecord]]:
        return iter(self._records.items())

    def awaiting_publication(self) -> Dict[str, PaperRecord]:
        """Announced papers whose journal publication has not been announced yet."""
        return {pid: record for pid, record in self._records.items() if record.posted and not record.published}

    def record_paper(self, paper: Paper, *, posted: Optional[bool] = None, published: Optional[bool] = None) -> PaperRecord:
        """Stores a paper's latest metadata and, optionally, sets its flags."""
        return self.update(
            paper.id,
            posted=posted,
            published=published,
            doi=paper.doi,
            journal_ref=paper.journal_ref,
            title=paper.title,
            authors=paper.authors,
        )

    def update(
        self,
        paper_id: str,
        *,
        posted: Optional[bool] = None,
        published: Optional[bool] = None,
        doi: Optional[str] = None,
        journal_ref: Optional[str] = None,
        title: Optional[str] = None,
        authors: Optional[List[str]] = None,
    ) -> PaperRecord:
        """
        Merges new information into a record. `posted`/`published` are sticky:
        once True they stay True (use `set_flags` to undo one deliberately), and
        empty metadata never overwrites what is already stored.
        """
        canonical_id = canonicalize_paper_id(paper_id)
        current = self._records.get(canonical_id, PaperRecord())
        cleaned_authors = [a.strip() for a in authors or [] if isinstance(a, str) and a.strip()]
        updated = PaperRecord(
            posted=current.posted or bool(posted),
            published=current.published or bool(published),
            doi=doi or current.doi,
            journal_ref=journal_ref or current.journal_ref,
            title=title or current.title,
            authors=cleaned_authors or list(current.authors),
        )
        if updated != current or canonical_id not in self._records:
            self._records[canonical_id] = updated
            self._dirty = True
        return updated

    def set_flags(self, paper_id: str, *, posted: Optional[bool] = None, published: Optional[bool] = None) -> PaperRecord:
        """Sets flags exactly as given (can turn them off), for manual corrections."""
        canonical_id = canonicalize_paper_id(paper_id)
        current = self._records.get(canonical_id, PaperRecord())
        updated = replace(
            current,
            posted=current.posted if posted is None else posted,
            published=current.published if published is None else published,
        )
        self._records[canonical_id] = updated
        self._dirty = True
        return updated

    def save(self):
        """Writes the registry if anything changed, atomically (temp file + rename)."""
        if not self._dirty:
            return
        if self.read_only:
            logger.info("Skipping save of tracked paper registry (--nosave).")
            return

        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            # Sort by paper ID so the file stays stable across runs.
            for paper_id in sorted(self._records):
                payload = {"id": paper_id, **asdict(self._records[paper_id])}
                f.write(json.dumps(payload, sort_keys=True))
                f.write("\n")
        os.replace(tmp_path, self.path)
        self._dirty = False


def _parse_line(line: str) -> Optional[Dict[str, Any]]:
    """Parses one JSON registry line into update() arguments plus "id";
    unreadable lines are skipped with a warning."""
    try:
        data = json.loads(line)
        paper_id = data.get("id")
        if not isinstance(paper_id, str):
            raise ValueError("JSON record missing string 'id'.")

        def text(key: str) -> Optional[str]:
            return clean_optional_text(data.get(key))

        authors = data.get("authors")
        return dict(
            id=paper_id,
            posted=bool(data.get("posted", True)),
            published=bool(data.get("published", False)),
            doi=text("doi"),
            journal_ref=text("journal_ref"),
            title=text("title"),
            authors=authors if isinstance(authors, list) else [],
        )
    except Exception as e:
        logger.warning(f"Skipping unreadable tracked-paper record '{line}': {e}")
        return None
