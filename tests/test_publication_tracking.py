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

from arxiv_source import ArxivSource, TRACKED_PAPER_REFRESH_RETRY_DELAYS_SECONDS
from crossref import CrossrefClient, choose_better_journal_ref, format_journal_ref
from discord_formatter import format_paper_message
from models import FetchPapersByIdResult, Paper, TrackedPaperMetadata
from publication_check import PublicationChecker
from registry import PaperRecord, PaperRegistry
from settings import AppSettings


@contextmanager
def workspace_tempdir():
    tmpdir = os.path.join(tempfile.gettempdir(), f"test-publication-{uuid4().hex}")
    os.makedirs(tmpdir, exist_ok=True)
    try:
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def make_settings(script_dir: str, **overrides) -> AppSettings:
    values = dict(
        target_authors=["Alice Example", "Bob Example"],
        author_discord_ids={"Alice Example": 12345},
        script_dir=script_dir,
    )
    values.update(overrides)
    return AppSettings(**values)


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


def make_checker(settings: AppSettings, refresh: FetchPapersByIdResult) -> PublicationChecker:
    checker = PublicationChecker(ArxivSource(settings), CrossrefClient())

    async def fake_fetch(_paper_ids):
        return refresh

    checker.arxiv.fetch_by_ids = fake_fetch  # type: ignore[method-assign]
    return checker


class RegistryTests(unittest.TestCase):
    def test_reads_records_and_skips_unreadable_lines(self):
        with workspace_tempdir() as tmpdir:
            path = make_settings(tmpdir).posted_papers_file
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"id": "2601.12345", "posted": True}) + "\n")
                handle.write("not json\n")
                handle.write(json.dumps({
                    "id": "2602.23456",
                    "posted": True,
                    "published": True,
                    "doi": "10.1000/example",
                    "journal_ref": "Journal of Tests",
                }))
                handle.write("\n")

            registry = PaperRegistry.load(path)

            self.assertEqual(len(registry), 2)
            self.assertEqual(registry.get("2601.12345"), PaperRecord(posted=True, published=False))
            self.assertEqual(registry.get("2602.23456").doi, "10.1000/example")
            self.assertTrue(registry.get("2602.23456").published)

    def test_update_and_save_round_trip(self):
        with workspace_tempdir() as tmpdir:
            path = make_settings(tmpdir).posted_papers_file
            registry = PaperRegistry.load(path)
            record = registry.update(
                "https://arxiv.org/abs/2603.99999v2",
                posted=True,
                published=True,
                doi="10.2000/final",
                journal_ref="Physics Letters",
                title="A Useful Quantum Paper",
                authors=["Alice Example", "Bob Example"],
            )
            self.assertTrue(record.posted and record.published)
            registry.save()

            reloaded = PaperRegistry.load(path).get("2603.99999")
            self.assertEqual(reloaded.journal_ref, "Physics Letters")
            self.assertEqual(reloaded.title, "A Useful Quantum Paper")
            self.assertEqual(reloaded.authors, ["Alice Example", "Bob Example"])

    def test_update_does_not_replace_authors_with_empty_update(self):
        with workspace_tempdir() as tmpdir:
            registry = PaperRegistry.load(make_settings(tmpdir).posted_papers_file)
            registry.update("2603.99999", posted=True, title="A Useful Quantum Paper", authors=["Alice Example", "Bob Example"])
            record = registry.update("2603.99999", published=True, doi="10.2000/final", authors=[])
            self.assertTrue(record.published)
            self.assertEqual(record.authors, ["Alice Example", "Bob Example"])

    def test_flags_are_sticky_except_through_set_flags(self):
        with workspace_tempdir() as tmpdir:
            registry = PaperRegistry.load(make_settings(tmpdir).posted_papers_file)
            registry.update("2603.99999", posted=True, published=True)
            self.assertTrue(registry.update("2603.99999", published=False).published)
            self.assertFalse(registry.set_flags("2603.99999", published=False).published)
            self.assertTrue(registry.get("2603.99999").posted)

    def test_read_only_registry_is_not_written(self):
        with workspace_tempdir() as tmpdir:
            path = make_settings(tmpdir).posted_papers_file
            registry = PaperRegistry.load(path, read_only=True)
            registry.update("2603.99999", posted=True)
            registry.save()
            self.assertFalse(os.path.exists(path))


class FormatterTests(unittest.TestCase):
    def test_format_publication_message_includes_doi(self):
        with workspace_tempdir() as tmpdir:
            paper = make_paper(doi="10.1000/example", journal_ref="Journal of Tests")
            message = format_paper_message(paper, make_settings(tmpdir), event_type="published")

            self.assertIsNotNone(message)
            self.assertIn("was published! Cheers!", message)
            self.assertIn("https://doi.org/10.1000/example", message)
            self.assertIn("New journal reference", message)
            self.assertIn("Journal of Tests", message)

    def test_format_message_mentions_canonical_author_for_initial_variant(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(
                tmpdir,
                target_authors=["Carla Bianca Rossi", "C. Bianca Rossi", "C Bianca Rossi"],
                author_discord_ids={"Carla Bianca Rossi": 54321, "C. Bianca Rossi": 54321, "C Bianca Rossi": 54321},
            )
            message = format_paper_message(make_paper(authors=["Carla B. Rossi", "Bob Example"]), settings)

            self.assertIsNotNone(message)
            self.assertIn("**New paper by <@54321>**", message)
            self.assertNotIn("tracked authors", message)
            self.assertEqual(message.count("<@54321>"), 1)


class AuthorMatchTests(unittest.TestCase):
    def test_accepts_middle_initial_variants(self):
        with workspace_tempdir() as tmpdir:
            source = ArxivSource(make_settings(tmpdir, target_authors=["Carla Bianca Rossi", "C. Bianca Rossi", "C Bianca Rossi"]))
            self.assertTrue(source.is_author_match(["Carla B. Rossi", "Coauthor Example"]))

    def test_rejects_different_full_name_matching_initial_alias(self):
        with workspace_tempdir() as tmpdir:
            source = ArxivSource(make_settings(tmpdir, target_authors=["Sebastian Kor", "S. Kor", "S Kor"]))
            self.assertFalse(source.is_author_match(["Sebastien Kor", "Coauthor Example"]))
            self.assertTrue(source.is_author_match(["S. Kor", "Coauthor Example"]))


class CrossrefMatchingTests(unittest.TestCase):
    def test_match_requires_close_title_and_author(self):
        client = CrossrefClient()
        paper = make_paper()
        base = {"type": "journal-article", "title": ["A Useful Quantum Paper"], "container-title": ["Quantum Journal"]}

        matched = client.publication_from_item(paper, {**base, "DOI": "10.3000/article", "author": [{"family": "Example"}]})
        rejected = client.publication_from_item(paper, {**base, "DOI": "10.3000/wrong", "author": [{"family": "Different"}]})

        self.assertIsNotNone(matched)
        self.assertEqual(matched.doi, "10.3000/article")
        self.assertIsNone(rejected)

    def test_formats_full_journal_reference(self):
        journal_ref = format_journal_ref({
            "container-title": ["Physical Review A"],
            "volume": "113",
            "issue": "3",
            "article-number": "032201",
            "published-print": {"date-parts": [[2026, 3, 1]]},
        })
        self.assertEqual(journal_ref, "Physical Review A 113.3 (2026): 032201")

    def test_choose_better_journal_ref_prefers_full_citation(self):
        chosen = choose_better_journal_ref("New Journal of Physics", "New Journal of Physics 28.1 (2026): 012345")
        self.assertEqual(chosen, "New Journal of Physics 28.1 (2026): 012345")


class PublicationCheckTests(unittest.TestCase):
    def test_reports_partial_refresh(self):
        with workspace_tempdir() as tmpdir:
            checker = make_checker(make_settings(tmpdir), FetchPapersByIdResult(
                papers=[make_paper(doi="10.1000/example", journal_ref="Journal of Tests")],
                requested_ids=["2603.99999", "2603.88888"],
                failed_ids=["2603.88888"],
                degraded=True,
            ))
            with patch.object(checker.crossref, "fetch_work_by_doi", return_value=None):
                result = asyncio.run(checker.find_updates(["2603.99999", "2603.88888"]))

            self.assertTrue(result.degraded)
            self.assertEqual(result.checked_ids, ["2603.99999"])
            self.assertEqual(result.failed_ids, ["2603.88888"])
            self.assertEqual(len(result.updates), 1)
            self.assertEqual(result.updates[0].announce_type, "published_metadata")

    def test_queries_crossref_for_unpublished_arxiv_record(self):
        with workspace_tempdir() as tmpdir:
            fetched_paper = make_paper()
            checker = make_checker(make_settings(tmpdir), FetchPapersByIdResult(
                papers=[fetched_paper], requested_ids=["2603.99999"], failed_ids=[], degraded=False,
            ))
            crossref_match = make_paper(doi="10.3000/article", journal_ref="Quantum Journal 12 (2026): 34", announce_type="crossref_published")

            with patch.object(checker.crossref, "find_publication", return_value=crossref_match) as mock_crossref:
                with self.assertLogs("PublicationChecker", level="INFO") as captured_logs:
                    result = asyncio.run(checker.find_updates(["2603.99999"]))

            self.assertEqual(result.checked_ids, ["2603.99999"])
            self.assertEqual(result.updates[0].doi, "10.3000/article")
            mock_crossref.assert_called_once_with(fetched_paper)
            self.assertIn("Publication update Crossref summary: 1 requests issued", "\n".join(captured_logs.output))

    def test_falls_back_to_crossref_for_failed_arxiv_refresh(self):
        with workspace_tempdir() as tmpdir:
            checker = make_checker(make_settings(tmpdir), FetchPapersByIdResult(
                papers=[], requested_ids=["2603.99999"], failed_ids=["2603.99999"], degraded=True,
            ))
            crossref_match = make_paper(doi="10.3000/article", journal_ref="Quantum Journal 12 (2026): 34", announce_type="crossref_published")
            tracked_metadata = {
                "2603.99999": TrackedPaperMetadata(
                    title="A Useful Quantum Paper", authors=["Alice Example", "Bob Example"], doi=None, journal_ref=None,
                )
            }

            with patch.object(checker.crossref, "find_publication", return_value=crossref_match) as mock_crossref:
                with self.assertLogs("PublicationChecker", level="INFO") as captured_logs:
                    result = asyncio.run(checker.find_updates(["2603.99999"], tracked_metadata=tracked_metadata))

            self.assertTrue(result.degraded)
            self.assertEqual(result.failed_ids, ["2603.99999"])
            self.assertEqual(result.updates[0].doi, "10.3000/article")
            mock_crossref.assert_called_once()
            self.assertIn("Crossref-only fallback attempted for 1 failed arXiv refreshes", "\n".join(captured_logs.output))


class ArxivRefreshTests(unittest.TestCase):
    def test_transient_failure_is_retried_then_marked_degraded(self):
        with workspace_tempdir() as tmpdir:
            source = ArxivSource(make_settings(tmpdir))

            with patch.object(source.client, "results", side_effect=Exception("HTTP 429")) as mock_results:
                with patch("arxiv_source.asyncio.sleep", new=AsyncMock()) as mock_sleep:
                    result = asyncio.run(source.fetch_by_ids(["2603.99999"]))

            self.assertTrue(result.degraded)
            self.assertEqual(result.papers, [])
            self.assertEqual(result.failed_ids, ["2603.99999"])
            self.assertEqual(mock_results.call_count, 3)
            self.assertEqual(
                [call.args[0] for call in mock_sleep.await_args_list],
                list(TRACKED_PAPER_REFRESH_RETRY_DELAYS_SECONDS),
            )


if __name__ == "__main__":
    unittest.main()
