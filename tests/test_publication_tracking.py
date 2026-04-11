import asyncio
import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from arxiv_fetcher import ArxivFetcher, FetchPapersByIdResult, Paper, TrackedPaperMetadata
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
                title="A Useful Quantum Paper",
                authors=["Alice Example", "Bob Example"],
            )

            self.assertTrue(record.posted)
            self.assertTrue(record.published)

            registry = manager.get_paper_registry()
            self.assertEqual(registry["2603.99999"].journal_ref, "Physics Letters")
            self.assertEqual(registry["2603.99999"].title, "A Useful Quantum Paper")
            self.assertEqual(registry["2603.99999"].authors, ["Alice Example", "Bob Example"])

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

    def test_fetch_publication_updates_reports_partial_refresh(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            fetcher = ArxivFetcher(settings)
            fetched_paper = make_paper(doi="10.1000/example", journal_ref="Journal of Tests")

            async def fake_fetch(_paper_ids):
                return FetchPapersByIdResult(
                    papers=[fetched_paper],
                    requested_ids=["2603.99999", "2603.88888"],
                    failed_ids=["2603.88888"],
                    degraded=True,
                )

            fetcher._fetch_papers_by_ids = fake_fetch  # type: ignore[method-assign]

            result = asyncio.run(fetcher.fetch_publication_updates(["2603.99999", "2603.88888"]))

            self.assertTrue(result.degraded)
            self.assertEqual(result.checked_ids, ["2603.99999"])
            self.assertEqual(result.failed_ids, ["2603.88888"])
            self.assertEqual(len(result.updates), 1)
            self.assertEqual(result.updates[0].announce_type, "published_metadata")

    def test_fetch_publication_updates_queries_crossref_for_unpublished_arxiv_record(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            fetcher = ArxivFetcher(settings)
            fetched_paper = make_paper()
            crossref_match = make_paper(
                doi="10.3000/article",
                journal_ref="Quantum Journal 12 (2026): 34",
                announce_type="crossref_published",
            )

            async def fake_fetch(_paper_ids):
                return FetchPapersByIdResult(
                    papers=[fetched_paper],
                    requested_ids=["2603.99999"],
                    failed_ids=[],
                    degraded=False,
                )

            fetcher._fetch_papers_by_ids = fake_fetch  # type: ignore[method-assign]

            with patch.object(fetcher, "_find_crossref_publication", return_value=crossref_match) as mock_crossref:
                result = asyncio.run(fetcher.fetch_publication_updates(["2603.99999"]))

            self.assertEqual(result.checked_ids, ["2603.99999"])
            self.assertEqual(len(result.updates), 1)
            self.assertEqual(result.updates[0].doi, "10.3000/article")
            mock_crossref.assert_called_once_with(fetched_paper)

    def test_fetch_publication_updates_logs_crossref_summary_for_unpublished_arxiv_record(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            fetcher = ArxivFetcher(settings)
            fetched_paper = make_paper()

            async def fake_fetch(_paper_ids):
                return FetchPapersByIdResult(
                    papers=[fetched_paper],
                    requested_ids=["2603.99999"],
                    failed_ids=[],
                    degraded=False,
                )

            fetcher._fetch_papers_by_ids = fake_fetch  # type: ignore[method-assign]

            with patch.object(fetcher, "_find_crossref_publication", return_value=None):
                with self.assertLogs("ArxivFetcher", level="INFO") as captured_logs:
                    asyncio.run(fetcher.fetch_publication_updates(["2603.99999"]))

            joined_logs = "\n".join(captured_logs.output)
            self.assertIn("Publication update Crossref summary: 1 requests issued", joined_logs)

    def test_fetch_publication_updates_falls_back_to_crossref_for_failed_arxiv_refresh(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            fetcher = ArxivFetcher(settings)
            crossref_match = make_paper(
                doi="10.3000/article",
                journal_ref="Quantum Journal 12 (2026): 34",
                announce_type="crossref_published",
            )

            async def fake_fetch(_paper_ids):
                return FetchPapersByIdResult(
                    papers=[],
                    requested_ids=["2603.99999"],
                    failed_ids=["2603.99999"],
                    degraded=True,
                )

            fetcher._fetch_papers_by_ids = fake_fetch  # type: ignore[method-assign]

            tracked_metadata = {
                "2603.99999": TrackedPaperMetadata(
                    title="A Useful Quantum Paper",
                    authors=["Alice Example", "Bob Example"],
                    doi=None,
                    journal_ref=None,
                )
            }

            with patch.object(fetcher, "_find_crossref_publication", return_value=crossref_match) as mock_crossref:
                result = asyncio.run(
                    fetcher.fetch_publication_updates(["2603.99999"], tracked_metadata=tracked_metadata)
                )

            self.assertTrue(result.degraded)
            self.assertEqual(result.failed_ids, ["2603.99999"])
            self.assertEqual(len(result.updates), 1)
            self.assertEqual(result.updates[0].doi, "10.3000/article")
            mock_crossref.assert_called_once()

    def test_fetch_publication_updates_logs_crossref_summary_for_failed_refresh(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            fetcher = ArxivFetcher(settings)

            async def fake_fetch(_paper_ids):
                return FetchPapersByIdResult(
                    papers=[],
                    requested_ids=["2603.99999"],
                    failed_ids=["2603.99999"],
                    degraded=True,
                )

            fetcher._fetch_papers_by_ids = fake_fetch  # type: ignore[method-assign]

            with patch.object(fetcher, "_find_crossref_publication", return_value=None):
                with self.assertLogs("ArxivFetcher", level="INFO") as captured_logs:
                    asyncio.run(
                        fetcher.fetch_publication_updates(
                            ["2603.99999"],
                            tracked_metadata={
                                "2603.99999": TrackedPaperMetadata(
                                    title="A Useful Quantum Paper",
                                    authors=["Alice Example", "Bob Example"],
                                    doi=None,
                                    journal_ref=None,
                                )
                            },
                        )
                    )

            joined_logs = "\n".join(captured_logs.output)
            self.assertIn("Publication update Crossref summary: 1 requests issued", joined_logs)
            self.assertIn("Crossref-only fallback attempted for 1 failed arXiv refreshes", joined_logs)

    def test_fetch_papers_by_ids_marks_transient_failure_as_degraded(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            fetcher = ArxivFetcher(settings)

            with patch.object(fetcher.arxiv_client, "results", side_effect=Exception("HTTP 429")) as mock_results:
                with patch("arxiv_fetcher.asyncio.sleep", new=AsyncMock()) as mock_sleep:
                    result = asyncio.run(fetcher._fetch_papers_by_ids(["2603.99999"]))

            self.assertTrue(result.degraded)
            self.assertEqual(result.papers, [])
            self.assertEqual(result.failed_ids, ["2603.99999"])
            self.assertEqual(mock_results.call_count, 3)
            self.assertEqual(mock_sleep.await_count, 2)


if __name__ == "__main__":
    unittest.main()
