"""Reads the arXiv RSS feed for some categories: the papers in the latest daily
announcement, and that announcement's date.

The feed only holds the latest mailing (and is empty until it is out), so the
bot uses it for each day's papers and the API search for any days it missed.
"""

import logging
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, NamedTuple, Optional

from pylatexenc.latex2text import LatexNodes2Text

from models import Paper, canonicalize_paper_id, clean_optional_text

# arXiv merges "a+b" feeds into one, listing a paper in both categories once.
RSS_URL = "https://rss.arxiv.org/rss/{categories}"
# 'replace' entries are new versions of older papers, not new papers.
NEW_PAPER_ANNOUNCE_TYPES = {"new", "cross"}
NAMESPACES = {
    "arxiv": "http://arxiv.org/schemas/atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}

logger = logging.getLogger("RssSource")
_latex = LatexNodes2Text()


class RssFeed(NamedTuple):
    # Announcement date in arXiv's (US Eastern) calendar. The feed carries it
    # even while still empty, so only a feed with papers counts as covering it.
    announced: Optional[date]
    papers: List[Paper]


def fetch_rss_feed(categories: List[str]) -> RssFeed:
    """Downloads and parses the feed; raises on network or HTTP errors."""
    url = rss_url(categories)
    request = urllib.request.Request(url, headers={"User-Agent": "arxiv-discord-bot/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        xml_text = response.read()
    feed = parse_feed(xml_text)
    if not feed.papers:
        logger.warning(f"RSS feed {url} has no new papers; the {feed.announced} announcement may not be out yet.")
    else:
        logger.info(f"RSS feed {url} lists {len(feed.papers)} new papers announced {feed.announced}.")
    return feed


def rss_url(categories: List[str]) -> str:
    return RSS_URL.format(categories="+".join(categories))


def parse_feed(xml_text) -> RssFeed:
    """Parses an arXiv RSS document, keeping only new announcements."""
    root = ET.fromstring(xml_text)
    pub_date = root.findtext("channel/pubDate")
    announced = parsedate_to_datetime(pub_date).date() if pub_date else None
    papers = []
    for item in root.iter("item"):
        try:
            paper = _paper_from_item(item)
        except Exception as e:
            logger.warning(f"Skipping unreadable RSS item: {e}")
            continue
        if paper:
            papers.append(paper)
    return RssFeed(announced, papers)


def previous_announcement_day(day: date) -> date:
    """The weekday before `day`: arXiv announces Monday to Friday."""
    previous = day - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous


def _paper_from_item(item: ET.Element) -> Optional[Paper]:
    announce_type = (item.findtext("arxiv:announce_type", "", NAMESPACES) or "").strip()
    if announce_type not in NEW_PAPER_ANNOUNCE_TYPES:
        return None

    link = (item.findtext("link") or "").strip()
    paper_id = canonicalize_paper_id(item.findtext("guid") or link)
    creators = item.findtext("dc:creator", "", NAMESPACES) or ""
    pub_date = item.findtext("pubDate")

    return Paper(
        id=paper_id,
        title=_decode(item.findtext("title") or ""),
        authors=[_decode(name) for name in creators.split(",") if name.strip()],
        # The feed only has the announcement date, not the submission date.
        published=parsedate_to_datetime(pub_date) if pub_date else datetime.now(timezone.utc),
        summary=_abstract(item.findtext("description") or ""),
        link=link or f"https://arxiv.org/abs/{paper_id}",
        pdf_link=f"https://arxiv.org/pdf/{paper_id}",
        doi=clean_optional_text(item.findtext("arxiv:DOI", None, NAMESPACES)),
        journal_ref=clean_optional_text(item.findtext("arxiv:journal_reference", None, NAMESPACES)),
    )


def _abstract(description: str) -> str:
    # Descriptions look like "arXiv:2611.00144v1 Announce Type: new \nAbstract: ...".
    _, marker, abstract = description.partition("Abstract:")
    return " ".join((abstract if marker else description).split())


def _decode(text: str) -> str:
    """Turns LaTeX accents and markup (e.g. R\\'enyi) into plain Unicode."""
    text = " ".join(text.split())
    try:
        return _latex.latex_to_text(text).strip()
    except Exception:
        return text
