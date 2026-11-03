"""Persistence helpers for the tracked-paper registry."""

import json
import os
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional, Set

from settings import AppSettings
from arxiv_fetcher import canonicalize_paper_id


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
    # ISO date of the last Crossref publication lookup (see publication_schedule.py).
    crossref_checked: Optional[str] = None

class StateManager:
    def __init__(self, settings: AppSettings):
        self.settings = settings

    def get_paper_registry(self) -> Dict[str, PaperRecord]:
        """Reads the registry of tracked papers from file."""
        file_path = self.settings.posted_papers_file
        if not os.path.exists(file_path):
            logging.info(f"{file_path} not found. Starting with an empty posted-papers registry.")
            return {}

        try:
            registry: Dict[str, PaperRecord] = {}
            with open(file_path, 'r', encoding='utf-8') as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line:
                        continue

                    # Parse one logical record per line so the file stays easy to
                    # inspect and recover manually if needed.
                    paper_id, record = self._parse_registry_line(line)
                    if not paper_id:
                        continue

                    # Merge duplicate lines defensively so repeated writes or
                    # legacy/manual edits do not lose information.
                    existing = registry.get(paper_id, PaperRecord())
                    registry[paper_id] = PaperRecord(
                        posted=existing.posted or record.posted,
                        published=existing.published or record.published,
                        doi=record.doi or existing.doi,
                        journal_ref=record.journal_ref or existing.journal_ref,
                        title=record.title or existing.title,
                        authors=record.authors or existing.authors,
                        crossref_checked=max(filter(None, (record.crossref_checked, existing.crossref_checked)), default=None),
                    )

            logging.info(f"Loaded {len(registry)} tracked paper records from {file_path}")
            return registry
        except Exception as e:
            logging.error(f"Error reading tracked paper records from {file_path}: {e}. Using empty registry.")
            return {}

    def get_posted_paper_ids(self) -> Set[str]:
        """Reads the set of already posted paper IDs from file."""
        return {
            paper_id
            for paper_id, record in self.get_paper_registry().items()
            if record.posted
        }

    def save_posted_paper_id(self, paper_id: str):
        """Marks a paper as posted in the registry file."""
        self.upsert_paper_record(paper_id, posted=True)

    def upsert_paper_record(
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
        """Creates or updates a tracked paper record and rewrites the registry file."""
        # The registry is small, so the simplest and safest approach is to
        # rewrite the full JSON-lines file on each update.
        registry = self.get_paper_registry()
        canonical_paper_id = canonicalize_paper_id(paper_id)
        current = registry.get(canonical_paper_id, PaperRecord())
        cleaned_authors = (
            [
                author.strip()
                for author in authors
                if isinstance(author, str) and author.strip()
            ]
            if authors is not None
            else None
        )
        updated = PaperRecord(
            # Treat posted/published as sticky flags: once True, never revert to
            # False just because a later call omitted that field.
            posted=current.posted or bool(posted),
            published=current.published or bool(published),
            doi=doi or current.doi,
            journal_ref=journal_ref or current.journal_ref,
            title=title or current.title,
            authors=cleaned_authors or list(current.authors),
            crossref_checked=current.crossref_checked,
        )
        registry[canonical_paper_id] = updated

        if self.settings.no_save:
            logging.info("Skipping save of tracked paper registry (--nosave).")
            return updated

        self._write_paper_registry(registry)
        logging.info(
            f"Updated tracked paper record {canonical_paper_id}: "
            f"posted={updated.posted}, published={updated.published}, doi={updated.doi}"
        )
        return updated

    def mark_crossref_checked(self, paper_ids: Iterable[str], checked_on: date) -> Dict[str, PaperRecord]:
        """Records a Crossref lookup date for several papers with a single file write."""
        registry = self.get_paper_registry()
        for paper_id in paper_ids:
            canonical_paper_id = canonicalize_paper_id(paper_id)
            if canonical_paper_id in registry:
                registry[canonical_paper_id].crossref_checked = checked_on.isoformat()
        if self.settings.no_save:
            logging.info("Skipping save of Crossref check dates (--nosave).")
        else:
            self._write_paper_registry(registry)
        return registry

    def _parse_registry_line(self, line: str) -> tuple[Optional[str], PaperRecord]:
        """Parses a registry line supporting both legacy IDs and JSON records."""
        if line.startswith("{"):
            try:
                data = json.loads(line)
                paper_id_raw = data.get("id")
                if not isinstance(paper_id_raw, str):
                    raise ValueError("JSON record missing string 'id'.")

                return canonicalize_paper_id(paper_id_raw), PaperRecord(
                    posted=bool(data.get("posted", True)),
                    published=bool(data.get("published", False)),
                    doi=data.get("doi") if isinstance(data.get("doi"), str) else None,
                    journal_ref=data.get("journal_ref") if isinstance(data.get("journal_ref"), str) else None,
                    title=data.get("title") if isinstance(data.get("title"), str) else None,
                    authors=[
                        author
                        for author in data.get("authors", [])
                        if isinstance(author, str)
                    ] if isinstance(data.get("authors"), list) else [],
                    crossref_checked=data.get("crossref_checked") if isinstance(data.get("crossref_checked"), str) else None,
                )
            except Exception as e:
                logging.warning(f"Skipping unreadable tracked-paper record '{line}': {e}")
                return None, PaperRecord()

        # Legacy format: one bare arXiv ID per line, which historically meant
        # only "this preprint was already posted".
        return canonicalize_paper_id(line), PaperRecord(posted=True)

    def _write_paper_registry(self, registry: Dict[str, PaperRecord]):
        """Writes the registry back to disk in a deterministic JSON-lines format."""
        file_path = self.settings.posted_papers_file
        # Write to a temp file and swap it in, so a crash (or the sync client
        # grabbing the file mid-write) never leaves a truncated registry.
        tmp_path = f"{file_path}.tmp"
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                # Sort by canonical paper ID so diffs stay stable across runs.
                for paper_id in sorted(registry):
                    record = registry[paper_id]
                    payload = {"id": paper_id, **asdict(record)}
                    f.write(json.dumps(payload, sort_keys=True))
                    f.write("\n")
            os.replace(tmp_path, file_path)
        except Exception as e:
            logging.error(f"Error saving tracked paper registry to {file_path}: {e}")
