"""Loads, expands, and persists the list of tracked authors.

The list lives in tracked_authors.json next to the bot so it can be edited by
hand and extended automatically by the website member sync. Each entry records
where it came from (`manual` or `website`) so auto-added names can be
reviewed, plus optional extra spellings and a Discord user ID for mentions.

Only full names and explicit variations are needed: the matcher in utils.py
already accepts initials ("F. Last") and middle names for every entry.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Dict, List, Optional

MANUAL_SOURCE = "manual"
WEBSITE_SOURCE = "website"


@dataclass
class TrackedAuthor:
    name: str
    source: str = MANUAL_SOURCE
    group: Optional[str] = None
    # Extra spellings the matcher cannot derive (e.g. a dropped middle name).
    variations: List[str] = field(default_factory=list)
    discord_id: Optional[int] = None
    added: Optional[str] = None
    website_slug: Optional[str] = None


@dataclass
class TrackedAuthorList:
    authors: List[TrackedAuthor] = field(default_factory=list)
    # Website member names that must never be auto-added (e.g. someone removed
    # by hand, or whose website name never matches their arXiv name).
    ignored_website_names: List[str] = field(default_factory=list)


def load_tracked_authors(path: str) -> TrackedAuthorList:
    """Reads the tracked-author file."""
    if not os.path.exists(path):
        raise ValueError(f"Tracked authors file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    authors = [
        TrackedAuthor(
            name=entry["name"],
            source=entry.get("source", MANUAL_SOURCE),
            group=entry.get("group"),
            variations=list(entry.get("variations", [])),
            discord_id=entry.get("discord_id"),
            added=entry.get("added"),
            website_slug=entry.get("website_slug"),
        )
        for entry in data.get("authors", [])
    ]
    return TrackedAuthorList(
        authors=authors,
        ignored_website_names=list(data.get("ignored_website_names", [])),
    )


def save_tracked_authors(path: str, tracked: TrackedAuthorList):
    """Writes the tracked-author file atomically so a crash never truncates it."""
    data = {
        "authors": [
            {key: value for key, value in asdict(author).items() if value not in (None, [])}
            for author in tracked.authors
        ],
        "ignored_website_names": tracked.ignored_website_names,
    }
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def author_name_variants(author: TrackedAuthor) -> List[str]:
    """Returns the canonical name followed by any extra spellings."""
    return _dedupe([author.name, *author.variations])


def expand_target_names(tracked: TrackedAuthorList) -> List[str]:
    """Flattens tracked authors into the name list the matcher works on."""
    names: List[str] = []
    for author in tracked.authors:
        names.extend(author_name_variants(author))
    return _dedupe(names)


def expand_discord_ids(tracked: TrackedAuthorList) -> Dict[str, int]:
    """Maps every name of each author with a Discord ID to that ID."""
    return {
        variant: author.discord_id
        for author in tracked.authors
        if author.discord_id
        for variant in author_name_variants(author)
    }


def new_website_author(name: str, slug: Optional[str], group: Optional[str]) -> TrackedAuthor:
    return TrackedAuthor(
        name=name,
        source=WEBSITE_SOURCE,
        group=group,
        added=date.today().isoformat(),
        website_slug=slug,
    )


def _dedupe(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
