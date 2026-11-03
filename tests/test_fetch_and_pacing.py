import asyncio
import io
import json
import os
import socket
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import arxiv_source
import crossref
from arxiv_source import ArxivSource
from crossref import CrossrefClient
from rss_source import RssFeed, parse_feed, read_last_covered, write_last_covered
from run_lock import LockHeldElsewhere, run_lock
from tests.test_publication_tracking import make_paper, make_settings, workspace_tempdir


TUESDAY, MONDAY, FRIDAY, THURSDAY = date(2026, 11, 3), date(2026, 11, 2), date(2026, 10, 30), date(2026, 10, 29)


class RecentPapersTests(unittest.TestCase):
    """RSS feed for the day's papers; API search only when something may be missing."""

    def run_search(self, *, feed=None, feed_error=False, last_covered=None, api_papers=(), api_complete=True, **overrides):
        with workspace_tempdir() as tmpdir:
            source = ArxivSource(make_settings(tmpdir, **overrides))
            if last_covered:
                write_last_covered(source.settings.rss_coverage_file, last_covered)
            seen_since = []

            async def fake_api(since):
                seen_since.append(since)
                return list(api_papers), api_complete

            source._search_since = fake_api  # type: ignore[method-assign]
            rss = dict(side_effect=OSError("down")) if feed_error else dict(return_value=feed)
            with patch.object(arxiv_source, "fetch_rss_feed", **rss):
                papers = asyncio.run(source.search_recent())
            source.commit_coverage()
            covered = read_last_covered(source.settings.rss_coverage_file)
        return [p.id for p in papers], seen_since, covered

    def test_consecutive_announcement_uses_only_the_feed(self):
        feed = RssFeed(TUESDAY, [make_paper(id="2611.00001"), make_paper(id="2611.00002", authors=["Someone Else"])])
        ids, seen_since, covered = self.run_search(feed=feed, last_covered=MONDAY)

        self.assertEqual(ids, ["2611.00001"])
        self.assertEqual(seen_since, [])
        self.assertEqual(covered, TUESDAY)

    def test_monday_follows_friday(self):
        _, seen_since, covered = self.run_search(feed=RssFeed(MONDAY, [make_paper()]), last_covered=FRIDAY)
        self.assertEqual(seen_since, [])
        self.assertEqual(covered, MONDAY)

    def test_missed_announcement_adds_api_search_from_before_the_gap(self):
        feed = RssFeed(MONDAY, [make_paper(id="2611.00001")])
        ids, seen_since, covered = self.run_search(
            feed=feed, last_covered=THURSDAY, api_papers=[make_paper(id="2610.99999"), make_paper(id="2611.00001")],
        )

        self.assertEqual(ids, ["2611.00001", "2610.99999"])
        self.assertLessEqual(seen_since[0].date(), THURSDAY - timedelta(days=5))
        self.assertEqual(covered, MONDAY)

    def test_empty_feed_uses_api_and_records_nothing(self):
        ids, seen_since, covered = self.run_search(
            feed=RssFeed(TUESDAY, []), last_covered=MONDAY, api_papers=[make_paper(id="2611.00001")], lookback_days=7,
        )
        self.assertEqual(ids, ["2611.00001"])
        age = datetime.now(timezone.utc) - seen_since[0]
        self.assertAlmostEqual(age.total_seconds(), timedelta(days=7).total_seconds(), delta=60)
        self.assertEqual(covered, MONDAY)

    def test_unreadable_feed_uses_api(self):
        ids, seen_since, covered = self.run_search(feed_error=True, last_covered=MONDAY, api_papers=[make_paper(id="2611.00001")])
        self.assertEqual(ids, ["2611.00001"])
        self.assertEqual(len(seen_since), 1)
        self.assertEqual(covered, MONDAY)

    def test_empty_or_unreadable_feed_notifies_when_falling_back(self):
        for rss in ({"feed": RssFeed(TUESDAY, [])}, {"feed_error": True}):
            with self.subTest(rss=rss), patch.object(arxiv_source, "send_telegram_message", return_value=False) as notify:
                _, searches, _ = self.run_search(last_covered=MONDAY, **rss)
                self.assertEqual(len(searches), 1)
                notify.assert_called_once()
                message = notify.call_args.args[1]
                self.assertIn("Falling back to the arXiv API", message)
                self.assertIn("quant-ph", message)

    def test_no_rss_failure_alert_for_healthy_feeds_or_rss_only(self):
        cases = [
            dict(feed=RssFeed(TUESDAY, [make_paper()]), last_covered=MONDAY),
            dict(feed=RssFeed(TUESDAY, [make_paper(authors=["Someone Else"])]), last_covered=MONDAY),
            dict(feed=RssFeed(TUESDAY, [make_paper()]), last_covered=THURSDAY),
            dict(feed=RssFeed(TUESDAY, [make_paper()]), last_covered=MONDAY, force_api=True),
            dict(feed=RssFeed(TUESDAY, []), rss_only=True),
        ]
        for case in cases:
            with self.subTest(case=case), patch.object(arxiv_source, "send_telegram_message") as notify:
                self.run_search(**case)
                notify.assert_not_called()

    def test_dry_run_does_not_deliver_fallback_notification(self):
        with patch("notifier.urllib.request.urlopen") as request:
            _, searches, _ = self.run_search(
                feed=RssFeed(TUESDAY, []), no_send=True, no_save=True,
                telegram_bot_token="test-token", telegram_chat_id="123",
            )
            self.assertEqual(len(searches), 1)
            request.assert_not_called()

    def test_incomplete_api_search_leaves_the_gap_open(self):
        _, _, covered = self.run_search(feed=RssFeed(MONDAY, [make_paper()]), last_covered=THURSDAY, api_complete=False)
        self.assertEqual(covered, THURSDAY)

    def test_rss_only_never_calls_the_api_and_keeps_the_gap_open(self):
        _, seen_since, covered = self.run_search(feed=RssFeed(MONDAY, [make_paper()]), last_covered=THURSDAY, rss_only=True)
        self.assertEqual(seen_since, [])
        self.assertEqual(covered, THURSDAY)

    def test_force_api_searches_even_without_a_gap(self):
        _, seen_since, covered = self.run_search(feed=RssFeed(TUESDAY, [make_paper()]), last_covered=MONDAY, force_api=True)
        self.assertEqual(len(seen_since), 1)
        self.assertEqual(covered, TUESDAY)

    def test_first_run_without_history_uses_api(self):
        _, seen_since, covered = self.run_search(feed=RssFeed(TUESDAY, [make_paper()]))
        self.assertEqual(len(seen_since), 1)
        self.assertEqual(covered, TUESDAY)

    def test_no_save_does_not_record(self):
        _, _, covered = self.run_search(feed=RssFeed(TUESDAY, [make_paper()]), last_covered=MONDAY, no_save=True)
        self.assertEqual(covered, MONDAY)


RSS_FEED = b"""<?xml version='1.0' encoding='UTF-8'?>
<rss xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel>
<pubDate>Tue, 03 Nov 2026 00:00:00 -0500</pubDate>
<item>
  <title>R\\'enyi entropies of things</title>
  <link>https://arxiv.org/abs/2611.00144</link>
  <description>arXiv:2611.00144v1 Announce Type: new
Abstract: An abstract.</description>
  <guid isPermaLink="false">oai:arXiv.org:2611.00144v1</guid>
  <pubDate>Tue, 03 Nov 2026 00:00:00 -0500</pubDate>
  <arxiv:announce_type>new</arxiv:announce_type>
  <dc:creator>Alice Example, Jos\\'e Mart\\'in</dc:creator>
  <arxiv:DOI>10.1/x</arxiv:DOI>
</item>
<item>
  <title>An old paper, new version</title>
  <link>https://arxiv.org/abs/2501.00001</link>
  <description>arXiv:2501.00001v3 Announce Type: replace
Abstract: Old.</description>
  <guid isPermaLink="false">oai:arXiv.org:2501.00001v3</guid>
  <pubDate>Tue, 03 Nov 2026 00:00:00 -0500</pubDate>
  <arxiv:announce_type>replace</arxiv:announce_type>
  <dc:creator>Alice Example</dc:creator>
</item>
</channel></rss>"""


class RssParsingTests(unittest.TestCase):
    def test_parses_new_entries_and_skips_replacements(self):
        feed = parse_feed(RSS_FEED)
        papers = feed.papers

        self.assertEqual(feed.announced, TUESDAY)

        self.assertEqual(len(papers), 1)
        paper = papers[0]
        self.assertEqual(paper.id, "2611.00144")
        self.assertEqual(paper.title, "Rényi entropies of things")
        self.assertEqual(paper.authors, ["Alice Example", "José Martín"])
        self.assertEqual(paper.summary, "An abstract.")
        self.assertEqual(paper.doi, "10.1/x")
        self.assertEqual(paper.announce_type, "rss_new")

    def test_empty_feed_gives_no_papers(self):
        feed = parse_feed(b"<rss version='2.0'><channel><pubDate>Tue, 03 Nov 2026 00:00:00 -0500</pubDate></channel></rss>")
        self.assertEqual(feed, RssFeed(TUESDAY, []))


class CrossrefPacingTests(unittest.TestCase):
    def test_retries_once_after_rate_limit(self):
        client = CrossrefClient()
        rate_limited = urllib.error.HTTPError("url", 429, "Too Many Requests", {"Retry-After": "2"}, None)
        ok = io.BytesIO(b'{"message": {}}')
        with patch.object(crossref.urllib.request, "urlopen", side_effect=[rate_limited, ok]), \
                patch.object(crossref.time, "sleep") as sleep:
            payload = client._get_json("https://example.org")
        self.assertEqual(payload, {"message": {}})
        self.assertIn(2.0, [call.args[0] for call in sleep.call_args_list])

    def test_other_errors_are_raised(self):
        client = CrossrefClient()
        server_error = urllib.error.HTTPError("url", 500, "Server Error", {}, None)
        with patch.object(crossref.urllib.request, "urlopen", side_effect=server_error):
            with self.assertRaises(urllib.error.HTTPError):
                client._get_json("https://example.org")


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
