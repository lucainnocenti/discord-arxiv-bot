"""Reads the arXiv RSS feed for a category: the papers in the latest daily
announcement. Used as a fallback when the API search fails.

The feed only holds the latest mailing (and is empty until it is out), which
is why the API's overlapping time window is the primary source.
"""

import logging
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional

from pylatexenc.latex2text import LatexNodes2Text

from models import Paper, canonicalize_paper_id, clean_optional_text

RSS_URL = "https://rss.arxiv.org/rss/{category}"
# 'replace' entries are new versions of older papers, not new papers.
NEW_PAPER_ANNOUNCE_TYPES = {"new", "cross"}
NAMESPACES = {
    "arxiv": "http://arxiv.org/schemas/atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}

logger = logging.getLogger("RssSource")
_latex = LatexNodes2Text()


def fetch_rss_papers(category: str) -> List[Paper]:
    """Downloads and parses the feed; raises on network or HTTP errors."""
    url = RSS_URL.format(category=category)
    request = urllib.request.Request(url, headers={"User-Agent": "arxiv-discord-bot/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        xml_text = response.read()
    papers = parse_feed(xml_text)
    if not papers:
        logger.warning(f"RSS feed {url} has no new papers; today's announcement may not be out yet.")
    else:
        logger.info(f"RSS feed {url} lists {len(papers)} new papers.")
    return papers


def parse_feed(xml_text) -> List[Paper]:
    """Parses an arXiv RSS document into Papers, keeping only new announcements."""
    root = ET.fromstring(xml_text)
    papers = []
    for item in root.iter("item"):
        try:
            paper = _paper_from_item(item)
        except Exception as e:
            logger.warning(f"Skipping unreadable RSS item: {e}")
            continue
        if paper:
            papers.append(paper)
    return papers


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
        announce_type=f"rss_{announce_type}",
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
