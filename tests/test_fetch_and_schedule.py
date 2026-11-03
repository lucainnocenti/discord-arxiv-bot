import asyncio
import io
import json
import os
import socket
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import arxiv_fetcher
from arxiv_fetcher import ArxivFetcher, FetchPapersByIdResult
from publication_schedule import is_crossref_check_due
from run_lock import LockHeldElsewhere, run_lock
from state_manager import StateManager
from tests.test_publication_tracking import make_paper, make_settings, workspace_tempdir


class RollingWindowFetchTests(unittest.TestCase):
    def test_queries_lookback_window_and_keeps_only_tracked_authors(self):
        with workspace_tempdir() as tmpdir:
            settings = make_settings(tmpdir)
            settings.lookback_days = 7
            fetcher = ArxivFetcher(settings)
            tracked = make_paper(id="2611.00001")
            namesake = make_paper(id="2611.00002", authors=["Alicia Exampleton"])
            seen_since = []

            async def fake_api(since):
                seen_since.append(since)
                return [tracked, namesake]

            fetcher._fetch_from_api = fake_api  # type: ignore[method-assign]
            papers = asyncio.run(fetcher.fetch_latest_papers())

            self.assertEqual([p.id for p in papers], ["2611.00001"])
            age = datetime.now(timezone.utc) - seen_since[0]
            self.assertAlmostEqual(age.total_seconds(), timedelta(days=7).total_seconds(), delta=60)


class CrossrefScheduleTests(unittest.TestCase):
    today = date(2026, 11, 3)

    def test_recent_papers_are_checked_weekly(self):
        self.assertTrue(is_crossref_check_due("2610.12015", None, self.today))
        self.assertFalse(is_crossref_check_due("2610.12015", "2026-10-30", self.today))
        self.assertTrue(is_crossref_check_due("2610.12015", "2026-10-27", self.today))

    def test_older_papers_are_checked_monthly_then_never(self):
        self.assertFalse(is_crossref_check_due("2407.09304", "2026-10-20", self.today))
        self.assertTrue(is_crossref_check_due("2407.09304", "2026-10-01", self.today))
        self.assertFalse(is_crossref_check_due("2308.04543", None, self.today))

    def test_unparseable_ids_are_skipped(self):
        self.assertFalse(is_crossref_check_due("quant-ph/0601001", None, self.today))

    def test_only_due_papers_get_crossref_lookups(self):
        with workspace_tempdir() as tmpdir:
            fetcher = ArxivFetcher(make_settings(tmpdir))
            due, not_due = make_paper(id="2611.00001"), make_paper(id="2611.00002")

            async def fake_fetch(_paper_ids):
                return FetchPapersByIdResult(papers=[due, not_due], requested_ids=[due.id, not_due.id], failed_ids=[], degraded=False)

            fetcher._fetch_papers_by_ids = fake_fetch  # type: ignore[method-assign]
            with patch.object(fetcher, "_find_crossref_publication", return_value=None) as crossref:
                result = asyncio.run(fetcher.fetch_publication_updates(
                    [due.id, not_due.id], crossref_due_ids={due.id},
                ))

            crossref.assert_called_once_with(due)
            self.assertEqual(result.checked_ids, [due.id, not_due.id])
            self.assertEqual(result.crossref_checked_ids, [due.id])

    def test_registry_records_crossref_check_date(self):
        with workspace_tempdir() as tmpdir:
            manager = StateManager(make_settings(tmpdir))
            manager.upsert_paper_record("2611.00001", posted=True)
            manager.mark_crossref_checked(["2611.00001", "2611.99999"], self.today)
            registry = manager.get_paper_registry()
            self.assertEqual(registry["2611.00001"].crossref_checked, "2026-11-03")
            self.assertNotIn("2611.99999", registry)
            # A later upsert must not wipe the check date.
            manager.upsert_paper_record("2611.00001", doi="10.1/x")
            self.assertEqual(manager.get_paper_registry()["2611.00001"].crossref_checked, "2026-11-03")


class CrossrefPacingTests(unittest.TestCase):
    def test_retries_once_after_rate_limit(self):
        with workspace_tempdir() as tmpdir:
            fetcher = ArxivFetcher(make_settings(tmpdir))
            rate_limited = urllib.error.HTTPError("url", 429, "Too Many Requests", {"Retry-After": "2"}, None)
            ok = io.BytesIO(b'{"message": {}}')
            with patch.object(arxiv_fetcher.urllib.request, "urlopen", side_effect=[rate_limited, ok]), \
                    patch.object(arxiv_fetcher.time, "sleep") as sleep:
                payload = fetcher._crossref_get_json("request")
            self.assertEqual(payload, {"message": {}})
            self.assertIn(2.0, [call.args[0] for call in sleep.call_args_list])

    def test_other_errors_are_raised(self):
        with workspace_tempdir() as tmpdir:
            fetcher = ArxivFetcher(make_settings(tmpdir))
            server_error = urllib.error.HTTPError("url", 500, "Server Error", {}, None)
            with patch.object(arxiv_fetcher.urllib.request, "urlopen", side_effect=server_error):
                with self.assertRaises(urllib.error.HTTPError):
                    fetcher._crossref_get_json("request")


class RunLockTests(unittest.TestCase):
    def write_lock(self, path, host, started):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"host": host, "pid": 1, "started": started.isoformat()}, f)

    def test_fresh_lock_from_another_host_blocks_the_run(self):
        with workspace_tempdir() as tmpdir:
            path = os.path.join(tmpdir, "run.lock")
            self.write_lock(path, "other-machine", datetime.now(timezone.utc))
            with self.assertRaises(LockHeldElsewhere):
                with run_lock(path):
                    pass

    def test_stale_or_own_lock_is_taken_and_released(self):
        with workspace_tempdir() as tmpdir:
            path = os.path.join(tmpdir, "run.lock")
            self.write_lock(path, "other-machine", datetime.now(timezone.utc) - timedelta(hours=2))
            with run_lock(path):
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["host"], socket.gethostname())
            self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
