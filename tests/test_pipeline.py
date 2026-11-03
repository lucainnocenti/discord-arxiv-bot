import asyncio
import contextlib
import io
import unittest
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import manage
from pipeline import Pipeline
from registry import PaperRegistry
from tests.test_publication_tracking import make_paper, make_settings, workspace_tempdir


class FakeSender:
    def __init__(self, succeed=True):
        self.messages = []
        self.succeed = succeed

    async def __call__(self, message):
        self.messages.append(message)
        return self.succeed


ANNOUNCED = date(2026, 11, 3)


def make_pipeline(tmpdir, *, new_papers=(), updates=(), succeed=True, **settings_overrides):
    settings = make_settings(tmpdir, **settings_overrides)
    registry = PaperRegistry.load(settings.posted_papers_file, read_only=settings.no_save)
    arxiv_source = MagicMock()
    arxiv_source.search_recent = AsyncMock(return_value=(list(new_papers), ANNOUNCED))
    publications = MagicMock()
    publications.enrich = AsyncMock(side_effect=lambda paper: paper)
    publications.find_updates = AsyncMock(return_value=list(updates))
    sender = FakeSender(succeed)
    return Pipeline(settings, registry, arxiv_source, publications, sender), sender


class PipelineTests(unittest.TestCase):
    def test_new_paper_is_posted_recorded_and_saved(self):
        with workspace_tempdir() as tmpdir:
            pipeline, sender = make_pipeline(tmpdir, new_papers=[make_paper(id="2611.00001")])
            summary = asyncio.run(pipeline.run())

            self.assertEqual(summary.papers_posted, 1)
            self.assertEqual(len(sender.messages), 1)
            self.assertIn("A Useful Quantum Paper", sender.messages[0])
            saved = PaperRegistry.load(pipeline.settings.posted_papers_file)
            self.assertTrue(saved.get("2611.00001").posted)

    def test_already_posted_paper_is_skipped_without_enrichment(self):
        with workspace_tempdir() as tmpdir:
            pipeline, sender = make_pipeline(tmpdir, new_papers=[make_paper(id="2611.00001")])
            pipeline.registry.update("2611.00001", posted=True, published=True)
            asyncio.run(pipeline.run())

            self.assertEqual(sender.messages, [])
            pipeline.publications.enrich.assert_not_awaited()

    def test_failed_send_is_not_recorded(self):
        with workspace_tempdir() as tmpdir:
            pipeline, sender = make_pipeline(tmpdir, new_papers=[make_paper(id="2611.00001")], succeed=False)
            summary = asyncio.run(pipeline.run())

            self.assertEqual(summary.papers_posted, 0)
            self.assertFalse(pipeline.registry.get("2611.00001").posted)
            # The announcement stays unprocessed, so the next run's API search retries it.
            pipeline.arxiv.record_coverage.assert_not_called()

    def test_announcement_is_recorded_once_everything_was_sent(self):
        with workspace_tempdir() as tmpdir:
            pipeline, _ = make_pipeline(tmpdir, new_papers=[make_paper(id="2611.00001")])
            asyncio.run(pipeline.run())
            pipeline.arxiv.record_coverage.assert_called_once_with(ANNOUNCED)

    def test_unformattable_paper_does_not_block_recording(self):
        with workspace_tempdir() as tmpdir:
            pipeline, sender = make_pipeline(tmpdir, new_papers=[make_paper(id="2611.00001", title="x" * 2100)])
            asyncio.run(pipeline.run())
            self.assertEqual(sender.messages, [])
            pipeline.arxiv.record_coverage.assert_called_once_with(ANNOUNCED)

    def test_no_send_dry_run_sends_and_records_nothing(self):
        with workspace_tempdir() as tmpdir:
            pipeline, sender = make_pipeline(tmpdir, new_papers=[make_paper(id="2611.00001")], no_send=True)
            asyncio.run(pipeline.run())

            self.assertEqual(sender.messages, [])
            self.assertNotIn("2611.00001", pipeline.registry)
            pipeline.arxiv.record_coverage.assert_not_called()

    def test_publication_update_is_announced_once(self):
        with workspace_tempdir() as tmpdir:
            published = make_paper(id="2610.00001", doi="10.1/x", journal_ref="Journal of Tests 1 (2026): 2")
            pipeline, sender = make_pipeline(tmpdir, updates=[published])
            pipeline.registry.update("2610.00001", posted=True, title="Old", authors=["Alice Example"])
            summary = asyncio.run(pipeline.run())

            self.assertEqual(summary.publication_updates, 1)
            self.assertIn("was published", sender.messages[0])
            self.assertTrue(PaperRegistry.load(pipeline.settings.posted_papers_file).get("2610.00001").published)

    def test_publication_check_runs_even_when_fetching_new_papers_fails(self):
        with workspace_tempdir() as tmpdir:
            pipeline, _ = make_pipeline(tmpdir)
            pipeline.arxiv.search_recent.side_effect = OSError("arXiv down")
            pipeline.registry.update("2610.00001", posted=True, title="Old", authors=["Alice Example"])
            asyncio.run(pipeline.run())

            pipeline.publications.find_updates.assert_awaited_once()
            pipeline.arxiv.record_coverage.assert_not_called()


class ManageCommandTests(unittest.TestCase):
    def run_manage(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = manage.main(list(argv))
        return code, out.getvalue()

    def test_set_can_clear_published_flag(self):
        with workspace_tempdir() as tmpdir:
            path = make_settings(tmpdir).posted_papers_file
            registry = PaperRegistry.load(path)
            registry.update("2605.15029", posted=True, published=True)
            registry.save()

            code, out = self.run_manage("--registry", path, "set", "2605.15029", "--unpublished")

            self.assertEqual(code, 0)
            record = PaperRegistry.load(path).get("2605.15029")
            self.assertTrue(record.posted)
            self.assertFalse(record.published)

    def test_list_awaiting_shows_only_unpublished(self):
        with workspace_tempdir() as tmpdir:
            path = make_settings(tmpdir).posted_papers_file
            registry = PaperRegistry.load(path)
            registry.update("2605.00001", posted=True)
            registry.update("2605.00002", posted=True, published=True)
            registry.save()

            _, out = self.run_manage("--registry", path, "list", "--awaiting")

            self.assertIn("2605.00001", out)
            self.assertNotIn("2605.00002", out)


if __name__ == "__main__":
    unittest.main()
