"""Persistence helpers for the bot's posting cursors and tracked-paper registry."""

import json
import os
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Dict, Optional, Set

from settings import AppSettings, EASTERN_TZ # Import shared settings and constants
from arxiv_fetcher import canonicalize_paper_id


@dataclass
class PaperRecord:
    # `posted` tracks whether the preprint announcement was sent.
    # `published` tracks whether the later journal-publication update was sent.
    posted: bool = False
    published: bool = False
    doi: Optional[str] = None
    journal_ref: Optional[str] = None

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
    ) -> PaperRecord:
        """Creates or updates a tracked paper record and rewrites the registry file."""
        # The registry is small, so the simplest and safest approach is to
        # rewrite the full JSON-lines file on each update.
        registry = self.get_paper_registry()
        canonical_paper_id = canonicalize_paper_id(paper_id)
        current = registry.get(canonical_paper_id, PaperRecord())
        updated = PaperRecord(
            # Treat posted/published as sticky flags: once True, never revert to
            # False just because a later call omitted that field.
            posted=current.posted or bool(posted),
            published=current.published or bool(published),
            doi=doi or current.doi,
            journal_ref=journal_ref or current.journal_ref,
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

    def get_last_api_check_time(self) -> datetime:
        """Reads the last API check date from file or returns a default."""
        if self.settings.last_date_override:
            logging.info(f"Using override date for API check: {self.settings.last_date_override}")
            return self.settings.last_date_override

        file_path = self.settings.last_submission_file
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r') as f:
                    date_str = f.read().strip()
                    dt = datetime.fromisoformat(date_str)
                    logging.info(f"Read last API check date from file: {dt}")
                    return dt
            except Exception as e:
                logging.error(f"Error reading last API check date from {file_path}: {e}. Using default (yesterday).")
                return self._default_past_date()
        else:
            logging.warning(f"{file_path} not found. Using default last check date (yesterday).")
            # Optionally create the file with the default date here if desired
            # self.save_last_api_check_time(self._default_past_date())
            return self._default_past_date()

    def save_last_api_check_time(self, time: datetime):
        """Saves the API check time to file."""
        if self.settings.no_save:
            logging.info("Skipping save of last API check time (--nosave).")
            return

        file_path = self.settings.last_submission_file
        try:
            # Add a small delta to avoid reprocessing the exact same timestamp
            # on the next inclusive arXiv API query.
            save_time = time + timedelta(seconds=1)
            with open(file_path, 'w') as f:
                f.write(save_time.isoformat())
            logging.info(f"Saved last API check time {save_time.isoformat()} to {file_path}")
        except Exception as e:
            logging.error(f"Error saving last API check time to {file_path}: {e}")

    def _get_last_rss_check_date_from_file(self) -> Optional[date]:
        """Reads the last RSS check date from its file."""
        file_path = self.settings.last_rss_check_file
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r') as f:
                    date_str = f.read().strip()
                    return datetime.fromisoformat(date_str).date()
            except Exception as e:
                logging.error(f"Error reading last RSS check date from {file_path}: {e}")
        return None # Indicate file not found or error

    def has_checked_rss_today(self) -> bool:
        """Checks if the RSS feed was already checked today (ET)."""
        if self.settings.force_rss_check:
            logging.info("Forcing RSS check (--forcerss).")
            return False

        last_check_date = self._get_last_rss_check_date_from_file()
        if last_check_date is None:
             logging.info(f"{self.settings.last_rss_check_file} not found or unreadable. Assuming RSS not checked today.")
             return False # Treat as not checked if file missing/error

        # The arXiv RSS feed is published on an Eastern Time cadence, so use the
        # same timezone when deciding whether "today's" poll already ran.
        current_date_et = datetime.now(EASTERN_TZ).date()
        has_checked = last_check_date == current_date_et
        if has_checked:
            logging.info(f"RSS feed already checked today ({current_date_et}).")
        else:
            logging.info(f"RSS feed not yet checked today ({current_date_et}). Last check was {last_check_date}.")
        return has_checked

    def save_rss_check_time(self):
        """Saves the current time (ET) as the last RSS check time."""
        if self.settings.no_save:
            logging.info("Skipping save of RSS check time (--nosave).")
            return

        file_path = self.settings.last_rss_check_file
        try:
            now_et = datetime.now(EASTERN_TZ)
            with open(file_path, 'w') as f:
                f.write(now_et.isoformat())
            logging.info(f"Saved current RSS check time {now_et.isoformat()} to {file_path}")
        except Exception as e:
            logging.error(f"Error saving RSS check time to {file_path}: {e}")

    def _default_past_date(self) -> datetime:
        """Returns a datetime object for yesterday."""
        # Use timezone-naive datetime for comparison with arXiv's naive datetimes
        return datetime.now() - timedelta(days=1)

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
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                # Sort by canonical paper ID so diffs stay stable across runs.
                for paper_id in sorted(registry):
                    record = registry[paper_id]
                    payload = {"id": paper_id, **asdict(record)}
                    f.write(json.dumps(payload, sort_keys=True))
                    f.write("\n")
        except Exception as e:
            logging.error(f"Error saving tracked paper registry to {file_path}: {e}")
