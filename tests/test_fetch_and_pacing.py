import asyncio
import io
import json
import os
import socket
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import arxiv_source
import crossref
from arxiv_source import ArxivSource
from crossref import CrossrefClient
from rss_source import parse_feed
from run_lock import LockHeldElsewhere, run_lock
from tests.test_publication_tracking import make_paper, make_settings, workspace_tempdir


class RecentPapersTests(unittest.TestCase):
    def run_search(self, *, api_papers=(), api_complete=True, rss_papers=(), **settings_overrides):
        with workspace_tempdir() as tmpdir:
            source = ArxivSource(make_settings(tmpdir, **settings_overrides))
            seen_since = []

            async def fake_api(since):
                seen_since.append(since)
                return list(api_papers), api_complete

            source._search_since = fake_api  # type: ignore[method-assign]
            with patch.object(arxiv_source, "fetch_rss_papers", return_value=list(rss_papers)) as rss:
                papers = asyncio.run(source.search_recent())
        return [p.id for p in papers], seen_since, rss

    def test_queries_lookback_window_and_keeps_only_tracked_authors(self):
        tracked = make_paper(id="2611.00001")
        namesake = make_paper(id="2611.00002", authors=["Alicia Exampleton"])
        ids, seen_since, rss = self.run_search(api_papers=[tracked, namesake], lookback_days=7)

        self.assertEqual(ids, ["2611.00001"])
        age = datetime.now(timezone.utc) - seen_since[0]
        self.assertAlmostEqual(age.total_seconds(), timedelta(days=7).total_seconds(), delta=60)
        rss.assert_not_called()

    def test_incomplete_api_search_falls_back_to_rss(self):
        ids, _, rss = self.run_search(
            api_papers=[make_paper(id="2611.00001")],
            api_complete=False,
            rss_papers=[make_paper(id="2611.00001"), make_paper(id="2611.00003"), make_paper(id="2611.00004", authors=["Someone Else"])],
        )
        rss.assert_called_once()
        self.assertEqual(ids, ["2611.00001", "2611.00003"])

    def test_rss_only_skips_the_api(self):
        ids, seen_since, _ = self.run_search(rss_papers=[make_paper(id="2611.00005")], rss_only=True)
        self.assertEqual(ids, ["2611.00005"])
        self.assertEqual(seen_since, [])


RSS_FEED = b"""<?xml version='1.0' encoding='UTF-8'?>
<rss xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel>
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
        papers = parse_feed(RSS_FEED)

        self.assertEqual(len(papers), 1)
        paper = papers[0]
        self.assertEqual(paper.id, "2611.00144")
        self.assertEqual(paper.title, "Rényi entropies of things")
        self.assertEqual(paper.authors, ["Alice Example", "José Martín"])
        self.assertEqual(paper.summary, "An abstract.")
        self.assertEqual(paper.doi, "10.1/x")
        self.assertEqual(paper.announce_type, "rss_new")

    def test_empty_feed_gives_no_papers(self):
        self.assertEqual(parse_feed(b"<rss version='2.0'><channel></channel></rss>"), [])


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
