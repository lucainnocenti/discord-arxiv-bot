import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from arxiv_fetcher import ArxivFetcher, Paper
from discord_formatter import format_paper_message
from settings import AppSettings
from state_manager import PaperRecord, StateManager


@contextmanager
def workspace_tempdir():
    tmpdir = os.path.join(tempfile.gettempdir(), f"test-publication-{uuid4().hex}")
    os.makedirs(tmpdir, exist_ok=True)
    try:
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def make_settings(script_dir: str) -> AppSettings:
    return AppSettings(
        discord_token="token",
        channel_id=1,
        test_channel_id=2,
        target_authors=["Alice Example", "Bob Example"],
        author_discord_ids={"Alice Example": 12345},
        script_dir=script_dir,
    )


def make_paper(**overrides) -> Paper:
    data = {
        "id": "2603.99999",
        "title": "A Useful Quantum Paper",
        "authors": ["Alice Example", "Bob Example"],
        "published": datetime(2026, 3, 1, tzinfo=ZoneInfo("UTC")),
        "summary": "Short abstract.",
        "link": "https://arxiv.org/abs/2603.99999",
        "pdf_link": "https://arxiv.org/pdf/2603.99999",
        "doi": None,
        "journal_ref": None,
        "announce_type": "api_new",
    }
    data.update(overrides)
    return Paper(**data)


class PublicationTrackingTests(unittest.TestCase):
    def test_registry_reads_legacy_and_json_records(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            with open(settings.posted_papers_file, "w", encoding="utf-8") as handle:
                handle.write("2601.12345\n")
                handle.write(json.dumps({
                    "id": "2602.23456",
                    "posted": True,
                    "published": True,
                    "doi": "10.1000/example",
                    "journal_ref": "Journal of Tests",
                }))
                handle.write("\n")

            registry = StateManager(settings).get_paper_registry()

            self.assertEqual(registry["2601.12345"], PaperRecord(posted=True, published=False))
            self.assertEqual(registry["2602.23456"].doi, "10.1000/example")
            self.assertTrue(registry["2602.23456"].published)

    def test_upsert_paper_record_rewrites_json_registry(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            manager = StateManager(settings)

            record = manager.upsert_paper_record(
                "https://arxiv.org/abs/2603.99999v2",
                posted=True,
                published=True,
                doi="10.2000/final",
                journal_ref="Physics Letters",
            )

            self.assertTrue(record.posted)
            self.assertTrue(record.published)

            registry = manager.get_paper_registry()
            self.assertEqual(registry["2603.99999"].journal_ref, "Physics Letters")

    def test_format_publication_message_includes_doi(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            paper = make_paper(doi="10.1000/example", journal_ref="Journal of Tests")

            message = format_paper_message(paper, settings, event_type="published")

            self.assertIsNotNone(message)
            self.assertIn("was published! Cheers!", message)
            self.assertIn("https://doi.org/10.1000/example", message)
            self.assertIn("New journal reference", message)
            self.assertIn("Journal of Tests", message)

    def test_crossref_match_requires_close_title_and_author(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            fetcher = ArxivFetcher(settings)
            paper = make_paper()

            matching_item = {
                "type": "journal-article",
                "DOI": "10.3000/article",
                "title": ["A Useful Quantum Paper"],
                "author": [{"family": "Example"}],
                "container-title": ["Quantum Journal"],
            }
            mismatching_item = {
                "type": "journal-article",
                "DOI": "10.3000/wrong",
                "title": ["A Useful Quantum Paper"],
                "author": [{"family": "Different"}],
                "container-title": ["Quantum Journal"],
            }

            matched = fetcher._crossref_item_to_publication_update(paper, matching_item)
            rejected = fetcher._crossref_item_to_publication_update(paper, mismatching_item)

            self.assertIsNotNone(matched)
            self.assertEqual(matched.doi, "10.3000/article")
            self.assertIsNone(rejected)

    def test_crossref_formats_full_journal_reference(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            fetcher = ArxivFetcher(settings)

            journal_ref = fetcher._format_crossref_journal_ref({
                "container-title": ["Physical Review A"],
                "volume": "113",
                "issue": "3",
                "article-number": "032201",
                "published-print": {"date-parts": [[2026, 3, 1]]},
            })

            self.assertEqual(journal_ref, "Physical Review A 113.3 (2026): 032201")

    def test_choose_better_journal_ref_prefers_full_citation(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            fetcher = ArxivFetcher(settings)

            chosen = fetcher._choose_better_journal_ref(
                "New Journal of Physics",
                "New Journal of Physics 28.1 (2026): 012345",
            )

            self.assertEqual(chosen, "New Journal of Physics 28.1 (2026): 012345")


if __name__ == "__main__":
    unittest.main()
