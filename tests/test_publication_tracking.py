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

from arxiv_api import RETRY_DELAYS_SECONDS, ArxivApi
from crossref import CrossrefClient, choose_better_journal_ref, format_journal_ref, publication_from_item
from discord_formatter import format_new_paper, format_publication
from models import Paper
from publication_check import PublicationChecker
from registry import PaperRecord, PaperRegistry
from settings import AppSettings
from tracked_authors import TrackedAuthor, TrackedAuthorList


@contextmanager
def workspace_tempdir():
    tmpdir = os.path.join(tempfile.gettempdir(), f"test-publication-{uuid4().hex}")
    os.makedirs(tmpdir, exist_ok=True)
    try:
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def tracked(*authors) -> TrackedAuthorList:
    """A tracked-author list; plain strings become authors without extras."""
    return TrackedAuthorList([TrackedAuthor(a) if isinstance(a, str) else a for a in authors])


DEFAULT_AUTHORS = tracked(TrackedAuthor("Alice Example", discord_id=12345), "Bob Example")


def make_settings(script_dir: str, **overrides) -> AppSettings:
    values = dict(authors=DEFAULT_AUTHORS, script_dir=script_dir)
    values.update(overrides)
    return AppSettings(**values)


def make_paper(**overrides) -> Paper:
    data = {
        "id": "2603.99999",
        "title": "A Useful Quantum Paper",
        "authors": ["Alice Example", "Bob Example"],
        "submitted": datetime(2026, 3, 1, tzinfo=ZoneInfo("UTC")),
        "summary": "Short abstract.",
        "link": "https://arxiv.org/abs/2603.99999",
        "doi": None,
        "journal_ref": None,
    }
    data.update(overrides)
    return Paper(**data)


def make_checker(settings: AppSettings, papers=(), failed_ids=()) -> PublicationChecker:
    checker = PublicationChecker(ArxivApi(), CrossrefClient())

    async def fake_fetch(_paper_ids):
        return list(papers), list(failed_ids)

    checker.api.fetch_by_ids = fake_fetch  # type: ignore[method-assign]
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
        paper = make_paper(doi="10.1000/example", journal_ref="Journal of Tests")
        message = format_publication(paper, DEFAULT_AUTHORS)

        self.assertIsNotNone(message)
        self.assertIn("was published! Cheers!", message)
        self.assertIn("https://doi.org/10.1000/example", message)
        self.assertIn("New journal reference", message)
        self.assertIn("Journal of Tests", message)

    def test_format_message_mentions_canonical_author_for_initial_variant(self):
        authors = tracked(TrackedAuthor("Carla Bianca Rossi", variations=["C. Bianca Rossi"], discord_id=54321))
        message = format_new_paper(make_paper(authors=["Carla B. Rossi", "Bob Example"]), authors)

        self.assertIsNotNone(message)
        self.assertIn("**New paper by <@54321>**", message)
        self.assertNotIn("tracked authors", message)
        self.assertEqual(message.count("<@54321>"), 1)

    def test_one_person_is_mentioned_once_under_two_spellings(self):
        authors = tracked(TrackedAuthor("Dana Holt", variations=["Dana A. Holt"], discord_id=7), "Bob Example", "Eve Example")
        paper = make_paper(authors=["Dana Holt", "Bob Example", "D. A. Holt", "Eve Example"])
        message = format_new_paper(paper, authors)
        self.assertIn("**New paper by <@7>, Bob Example, and Eve Example**", message)

    def test_paper_without_tracked_authors_names_none(self):
        message = format_new_paper(make_paper(authors=["Someone Else"]), DEFAULT_AUTHORS)
        self.assertIn("**New paper by tracked authors**", message)


class AuthorMatchTests(unittest.TestCase):
    def test_accepts_middle_initial_variants(self):
        authors = tracked("Carla Bianca Rossi")
        self.assertTrue(authors.found_in(["Carla B. Rossi", "Coauthor Example"]))
        self.assertTrue(authors.found_in(["C. B. Rossi"]))

    def test_rejects_different_full_name_matching_initial_alias(self):
        authors = tracked(TrackedAuthor("Sebastian Kor", variations=["S. Kor"]))
        self.assertFalse(authors.found_in(["Sebastien Kor", "Coauthor Example"]))
        self.assertTrue(authors.found_in(["S. Kor", "Coauthor Example"]))

    def test_found_in_returns_people_in_paper_order(self):
        authors = tracked("Alice Example", "Bob Example")
        alice, bob = authors.authors
        self.assertEqual(authors.found_in(["B. Example", "Someone", "Alice Example"]), [bob, alice])


class CrossrefMatchingTests(unittest.TestCase):
    def test_match_requires_close_title_and_author(self):
        paper = make_paper()
        base = {"type": "journal-article", "title": ["A Useful Quantum Paper"], "container-title": ["Quantum Journal"]}

        matched = publication_from_item(paper, {**base, "DOI": "10.3000/article", "author": [{"family": "Example"}]})
        rejected = publication_from_item(paper, {**base, "DOI": "10.3000/wrong", "author": [{"family": "Different"}]})

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
    RECORD = PaperRecord(posted=True, title="A Useful Quantum Paper", authors=["Alice Example", "Bob Example"])

    def test_arxiv_publication_metadata_is_an_update(self):
        with workspace_tempdir() as tmpdir:
            checker = make_checker(make_settings(tmpdir), papers=[make_paper(doi="10.1000/example", journal_ref="Journal of Tests")])
            with patch.object(checker.crossref, "fetch_work_by_doi", return_value=None),                     patch.object(checker.crossref, "find_publication", return_value=None) as search:
                updates = asyncio.run(checker.find_updates({"2603.99999": self.RECORD}))

            self.assertEqual([p.doi for p in updates], ["10.1000/example"])
            # The journal ref is sparse, so Crossref is asked for a better one.
            search.assert_called_once()

    def test_queries_crossref_for_unpublished_arxiv_record(self):
        with workspace_tempdir() as tmpdir:
            fetched_paper = make_paper()
            checker = make_checker(make_settings(tmpdir), papers=[fetched_paper])
            crossref_match = make_paper(doi="10.3000/article", journal_ref="Quantum Journal 12 (2026): 34")

            with patch.object(checker.crossref, "find_publication", return_value=crossref_match) as mock_crossref:
                updates = asyncio.run(checker.find_updates({"2603.99999": self.RECORD}))

            self.assertEqual([p.doi for p in updates], ["10.3000/article"])
            mock_crossref.assert_called_once_with(fetched_paper)

    def test_unpublished_paper_gives_no_update(self):
        with workspace_tempdir() as tmpdir:
            checker = make_checker(make_settings(tmpdir), papers=[make_paper()])
            with patch.object(checker.crossref, "find_publication", return_value=None):
                self.assertEqual(asyncio.run(checker.find_updates({"2603.99999": self.RECORD})), [])

    def test_falls_back_to_crossref_for_failed_arxiv_refresh(self):
        with workspace_tempdir() as tmpdir:
            checker = make_checker(make_settings(tmpdir), failed_ids=["2603.99999"])
            crossref_match = make_paper(doi="10.3000/article", journal_ref="Quantum Journal 12 (2026): 34")

            with patch.object(checker.crossref, "find_publication", return_value=crossref_match) as mock_crossref:
                updates = asyncio.run(checker.find_updates({"2603.99999": self.RECORD}))

            self.assertEqual([p.doi for p in updates], ["10.3000/article"])
            searched = mock_crossref.call_args.args[0]
            self.assertEqual((searched.id, searched.title, searched.authors), ("2603.99999", self.RECORD.title, self.RECORD.authors))


class ArxivRefreshTests(unittest.TestCase):
    def test_transient_failure_is_retried_then_reported_as_failed(self):
        api = ArxivApi()
        with patch.object(api.client, "results", side_effect=Exception("HTTP 429")) as mock_results:
            with patch("arxiv_api.asyncio.sleep", new=AsyncMock()) as mock_sleep:
                papers, failed_ids = asyncio.run(api.fetch_by_ids(["2603.99999"]))

        self.assertEqual(papers, [])
        self.assertEqual(failed_ids, ["2603.99999"])
        self.assertEqual(mock_results.call_count, 3)
        self.assertEqual([call.args[0] for call in mock_sleep.await_args_list], list(RETRY_DELAYS_SECONDS))

    def test_failed_chunk_stops_the_refresh(self):
        api = ArxivApi()
        ids = [f"2603.{n:05d}" for n in range(15)]
        with patch.object(api.client, "results", side_effect=Exception("HTTP 400")) as mock_results:
            papers, failed_ids = asyncio.run(api.fetch_by_ids(ids))

        self.assertEqual(papers, [])
        self.assertEqual(failed_ids, ids)
        mock_results.assert_called_once()

if __name__ == "__main__":
    unittest.main()
