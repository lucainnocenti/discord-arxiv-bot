import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import member_sync
from member_sync import WebsiteMember, find_untracked_members, parse_people_page, sync_members_from_website
from settings import AppSettings
from tracked_authors import (
    MANUAL_SOURCE,
    WEBSITE_SOURCE,
    TrackedAuthor,
    TrackedAuthorList,
    expand_discord_ids,
    expand_target_names,
    load_tracked_authors,
    save_tracked_authors,
)


def person_card(name, slug):
    return (
        '<article class="card person"><div class=person-photo></div><div class=card-body>'
        f'<h3 class=card-title><a class=stretched href=/people/{slug}/>{name}</a></h3>'
        '<p class=person-role>Role</p></div></article>'
    )


def people_page(current, alumni=("Former Member",)):
    cards = "".join(person_card(name, name.lower().replace(" ", "-")) for name in current)
    alumni_html = "".join(f"<li>{name}</li>" for name in alumni)
    return (
        '<section class=people-group id=phd aria-labelledby=phd-title><h2>PhD students</h2>'
        f'<div class="grid grid-people">{cards}</div></section>'
        f'<section class=people-group id=alumni><h2>Former members</h2><ul>{alumni_html}</ul></section>'
    )


FILLER = [f"Filler Person{i}" for i in range(10)]


class PeoplePageParsingTests(unittest.TestCase):
    def test_parses_current_member_cards_and_skips_alumni(self):
        members = parse_people_page(people_page(["Nora Lindqvist", *FILLER]))
        self.assertIn(WebsiteMember("Nora Lindqvist", "phd", "nora-lindqvist"), members)
        self.assertNotIn("Former Member", [m.name for m in members])

    def test_rejects_page_with_too_few_members(self):
        with self.assertRaises(ValueError):
            parse_people_page(people_page(["Only One"]))


class UntrackedMemberTests(unittest.TestCase):
    def test_known_variants_and_ignored_names_are_not_new(self):
        tracked = TrackedAuthorList(
            authors=[TrackedAuthor("Enzo Ferri", variations=["R. Enzo Ferri"])],
            ignored_website_names=["Ignored Person"],
        )
        members = [
            WebsiteMember("R. Enzo Ferri", "faculty", None),
            WebsiteMember("Ignored Person", "postdoc", None),
            WebsiteMember("New Person", "phd", None),
        ]
        self.assertEqual([m.name for m in find_untracked_members(members, tracked)], ["New Person"])


class TrackedAuthorFileTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"test-authors-{uuid4().hex}")
        os.makedirs(self.tmpdir)
        self.path = os.path.join(self.tmpdir, "tracked_authors.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_round_trip_keeps_source_tags(self):
        tracked = TrackedAuthorList(authors=[
            TrackedAuthor("Alice Example", group="phd"),
            TrackedAuthor("Bob Example", source=WEBSITE_SOURCE, added="2026-11-03", website_slug="bob-example"),
        ])
        save_tracked_authors(self.path, tracked)
        loaded = load_tracked_authors(self.path)
        self.assertEqual([a.source for a in loaded.authors], [MANUAL_SOURCE, WEBSITE_SOURCE])
        self.assertEqual(loaded.authors[1].website_slug, "bob-example")

    def test_bootstraps_from_legacy_config(self):
        legacy = SimpleNamespace(names_variations={
            "Dana Holt": ["D. Holt", "D Holt", "Dana A. Holt"],
        })
        loaded = load_tracked_authors(self.path, legacy_config=legacy)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(loaded.authors[0].variations, ["Dana A. Holt"])
        self.assertEqual(
            expand_target_names(loaded),
            ["Dana Holt", "D. Holt", "D Holt", "Dana A. Holt"],
        )

    def test_discord_ids_cover_name_variants(self):
        tracked = TrackedAuthorList(authors=[TrackedAuthor("Alice Example")])
        ids = expand_discord_ids(tracked, {"Alice Example": 7})
        self.assertEqual(ids["A. Example"], 7)


class SyncFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"test-sync-{uuid4().hex}")
        os.makedirs(self.tmpdir)
        self.settings = AppSettings(
            discord_token="token", channel_id=1, test_channel_id=2,
            target_authors=[], author_discord_ids={}, script_dir=self.tmpdir,
            members_page_url="https://example.org/people/",
        )
        save_tracked_authors(
            self.settings.tracked_authors_file,
            TrackedAuthorList(authors=[TrackedAuthor(name) for name in FILLER]),
        )
        self.members = [WebsiteMember(name, "phd", None) for name in FILLER]
        self.members.append(WebsiteMember("New Person", "postdoc", "new-person"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_sync(self):
        with patch.object(member_sync, "fetch_website_members", return_value=self.members), \
                patch.object(member_sync, "send_telegram_message") as notify:
            added = sync_members_from_website(self.settings)
        return added, notify

    def test_adds_new_member_tagged_as_website_and_notifies(self):
        added, notify = self.run_sync()
        self.assertEqual(added, ["New Person"])
        new_author = load_tracked_authors(self.settings.tracked_authors_file).authors[-1]
        self.assertEqual((new_author.name, new_author.source, new_author.group), ("New Person", WEBSITE_SOURCE, "postdoc"))
        notify.assert_called_once()
        self.assertIn("New Person", notify.call_args.args[1])

    def test_runs_at_most_weekly(self):
        self.run_sync()
        self.members.append(WebsiteMember("Later Person", "phd", None))
        added, _ = self.run_sync()
        self.assertEqual(added, [])

        with open(self.settings.last_member_sync_file, "w") as f:
            f.write((date.today() - timedelta(days=7)).isoformat())
        added, _ = self.run_sync()
        self.assertEqual(added, ["Later Person"])

    def test_disabled_and_no_save_leave_files_untouched(self):
        self.settings.member_sync_enabled = False
        self.assertEqual(self.run_sync()[0], [])
        self.settings.member_sync_enabled = True
        self.settings.no_save = True
        self.assertEqual(self.run_sync()[0], [])
        self.assertFalse(os.path.exists(self.settings.last_member_sync_file))
        self.assertEqual(len(load_tracked_authors(self.settings.tracked_authors_file).authors), len(FILLER))

    def test_fetch_failure_is_retried_next_run(self):
        with patch.object(member_sync, "fetch_website_members", side_effect=OSError("down")):
            self.assertEqual(sync_members_from_website(self.settings), [])
        self.assertFalse(os.path.exists(self.settings.last_member_sync_file))


if __name__ == "__main__":
    unittest.main()
