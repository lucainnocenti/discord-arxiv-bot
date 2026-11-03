"""Loads, expands, and persists the list of tracked authors.

The list lives in a JSON file next to the bot so it can be edited by hand and
extended automatically by the website member sync. Each entry records where
it came from (`manual` or `website`) so auto-added names can be reviewed.
"""

import json
import logging
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
    # Extra spellings beyond the automatic "F. Last" / "F Last" forms.
    variations: List[str] = field(default_factory=list)
    added: Optional[str] = None
    website_slug: Optional[str] = None


@dataclass
class TrackedAuthorList:
    authors: List[TrackedAuthor] = field(default_factory=list)
    # Website member names that must never be auto-added (e.g. someone removed
    # by hand, or whose website name never matches their arXiv name).
    ignored_website_names: List[str] = field(default_factory=list)


def load_tracked_authors(path: str, legacy_config=None) -> TrackedAuthorList:
    """Reads the tracked-author file, bootstrapping it from config.py if missing."""
    if not os.path.exists(path):
        if legacy_config is None or not hasattr(legacy_config, "names_variations"):
            raise ValueError(f"Tracked authors file not found: {path}")
        tracked = _from_legacy_config(legacy_config)
        save_tracked_authors(path, tracked)
        logging.info(f"Created {path} from the author list in config.py ({len(tracked.authors)} authors).")
        return tracked

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    authors = [
        TrackedAuthor(
            name=entry["name"],
            source=entry.get("source", MANUAL_SOURCE),
            group=entry.get("group"),
            variations=list(entry.get("variations", [])),
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
    """Returns the canonical name, its initial forms, and any extra variations."""
    variants = [author.name]
    parts = author.name.split()
    if len(parts) >= 2:
        rest = " ".join(parts[1:])
        variants.append(f"{parts[0][0]}. {rest}")
        variants.append(f"{parts[0][0]} {rest}")
    variants.extend(author.variations)
    return _dedupe(variants)


def expand_target_names(tracked: TrackedAuthorList) -> List[str]:
    """Flattens tracked authors into the name list the matcher works on."""
    names: List[str] = []
    for author in tracked.authors:
        names.extend(author_name_variants(author))
    return _dedupe(names)


def expand_discord_ids(tracked: TrackedAuthorList, discord_ids: Dict[str, int]) -> Dict[str, int]:
    """Maps every name variant of a tracked author to that author's Discord ID."""
    expanded = dict(discord_ids)
    for author in tracked.authors:
        if author.name not in discord_ids:
            continue
        for variant in author_name_variants(author):
            expanded.setdefault(variant, discord_ids[author.name])
    return expanded


def new_website_author(name: str, slug: Optional[str], group: Optional[str]) -> TrackedAuthor:
    return TrackedAuthor(
        name=name,
        source=WEBSITE_SOURCE,
        group=group,
        added=date.today().isoformat(),
        website_slug=slug,
    )


def _from_legacy_config(legacy_config) -> TrackedAuthorList:
    # Old config.py files expand TARGET_AUTHORS in place, but `names_variations`
    # keeps the canonical names (as keys) in their original order.
    authors = []
    for name, variations in legacy_config.names_variations.items():
        author = TrackedAuthor(name=name)
        auto_variants = set(author_name_variants(author))
        author.variations = [v for v in variations if v not in auto_variants]
        authors.append(author)
    return TrackedAuthorList(authors=authors)


def _dedupe(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
