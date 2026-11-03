"""Weekly sync of current group members from the group website.

New current members that no tracked author matches are appended to the
tracked-author file with source "website". The sync only ever adds: people who
leave the group stay tracked, and names in `ignored_website_names` are skipped.
"""

import logging
import os
import urllib.request
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import List, NamedTuple, Optional

from notifier import send_telegram_message
from settings import AppSettings
from tracked_authors import (
    TrackedAuthorList,
    expand_target_names,
    load_tracked_authors,
    new_website_author,
    save_tracked_authors,
)
from utils import find_matching_target_authors

PEOPLE_PAGE_URL = "https://quantum.unipa.it/people/"
MEMBER_SYNC_INTERVAL = timedelta(days=7)
# Former members are listed as plain names, not cards, but skip the section
# explicitly in case the markup changes.
EXCLUDED_SECTIONS = {"alumni"}
# A redesign or a broken page should not look like "the group is empty".
MIN_EXPECTED_MEMBERS = 10

logger = logging.getLogger("MemberSync")


class WebsiteMember(NamedTuple):
    name: str
    group: Optional[str]  # Section id on the people page, e.g. "phd"
    slug: Optional[str]   # Profile path segment, e.g. "markos-arapchatzis"


class _PeoplePageParser(HTMLParser):
    """Collects names from `section.people-group > article.person h3.card-title a`."""

    def __init__(self):
        super().__init__()
        self.members: List[WebsiteMember] = []
        self.sections_seen: List[str] = []
        self._section: Optional[str] = None
        self._in_person = False
        self._in_title = False
        self._in_link = False
        self._href: Optional[str] = None
        self._text: List[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "section" and "people-group" in classes:
            self._section = attrs.get("id")
            self.sections_seen.append(self._section or "")
        elif tag == "article" and "person" in classes:
            self._in_person = True
        elif tag == "h3" and self._in_person and "card-title" in classes:
            self._in_title = True
        elif tag == "a" and self._in_title:
            self._in_link = True
            self._href = attrs.get("href")
            self._text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_link:
            self._in_link = False
            name = " ".join("".join(self._text).split())
            if name and self._section not in EXCLUDED_SECTIONS:
                self.members.append(WebsiteMember(name, self._section, _slug_from_href(self._href)))
        elif tag == "h3":
            self._in_title = False
        elif tag == "article":
            self._in_person = False
        elif tag == "section":
            self._section = None

    def handle_data(self, data):
        if self._in_link:
            self._text.append(data)


def parse_people_page(html: str) -> List[WebsiteMember]:
    """Extracts current members from the people page, failing loudly on odd markup."""
    parser = _PeoplePageParser()
    parser.feed(html)
    if len(parser.members) < MIN_EXPECTED_MEMBERS:
        raise ValueError(
            f"People page yielded only {len(parser.members)} members "
            f"(sections seen: {parser.sections_seen}); the page layout may have changed."
        )
    return parser.members


def fetch_website_members(url: str = PEOPLE_PAGE_URL) -> List[WebsiteMember]:
    request = urllib.request.Request(url, headers={"User-Agent": "qunipa-arxiv-bot"})
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")
    return parse_people_page(html)


def find_untracked_members(members: List[WebsiteMember], tracked: TrackedAuthorList) -> List[WebsiteMember]:
    """Returns website members that no tracked author (or ignored name) covers."""
    tracked_names = expand_target_names(tracked) + tracked.ignored_website_names
    return [
        member
        for member in members
        if not find_matching_target_authors([member.name], tracked_names)
    ]


def is_member_sync_due(settings: AppSettings) -> bool:
    if settings.force_member_sync:
        return True
    last_sync = _read_last_sync_date(settings.last_member_sync_file)
    if last_sync is None:
        return True
    return date.today() - last_sync >= MEMBER_SYNC_INTERVAL


def sync_members_from_website(settings: AppSettings) -> List[str]:
    """
    Adds untracked current website members to the tracked-author file when the
    weekly sync is due. Returns the names that were added.

    Errors are logged and swallowed: a website problem must never stop the bot
    from posting papers. The sync date is only saved after a successful fetch,
    so a failed sync is retried on the next run.
    """
    if not settings.member_sync_enabled:
        logger.info("Website member sync disabled (--nomembersync).")
        return []
    if not is_member_sync_due(settings):
        logger.info("Website member sync not due yet (runs weekly).")
        return []

    try:
        members = fetch_website_members()
    except Exception as e:
        logger.warning(f"Website member sync failed; will retry next run: {e}")
        return []

    tracked = load_tracked_authors(settings.tracked_authors_file)
    untracked = find_untracked_members(members, tracked)
    logger.info(f"Website lists {len(members)} current members; {len(untracked)} not yet tracked.")

    if settings.no_save:
        for member in untracked:
            logger.info(f"[NO_SAVE] Would add website member '{member.name}' ({member.group}).")
        return []

    for member in untracked:
        tracked.authors.append(new_website_author(member.name, member.slug, member.group))
        logger.info(f"Added website member '{member.name}' ({member.group}) to tracked authors.")
    if untracked:
        save_tracked_authors(settings.tracked_authors_file, tracked)
        _notify_new_authors(settings, untracked)

    _write_last_sync_date(settings.last_member_sync_file)
    return [member.name for member in untracked]


def _notify_new_authors(settings: AppSettings, added: List[WebsiteMember]):
    lines = [f"arxiv-bot: added {len(added)} new tracked author(s) from {PEOPLE_PAGE_URL}"]
    lines += [f"- {member.name} ({member.group or 'unknown role'})" for member in added]
    lines.append("Check their arXiv name forms in tracked_authors.json.")
    send_telegram_message(settings, "\n".join(lines))


def _read_last_sync_date(path: str) -> Optional[date]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return datetime.fromisoformat(f.read().strip()).date()
    except Exception as e:
        logger.warning(f"Could not read last member sync date from {path}: {e}")
        return None


def _write_last_sync_date(path: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(date.today().isoformat())


def _slug_from_href(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    parts = [part for part in href.split("/") if part]
    return parts[-1] if parts else None
