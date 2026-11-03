"""Reading and writing the small one-date state files next to the bot."""

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


def read_date(path: str) -> Optional[date]:
    """The date stored in `path`, or None if the file is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return date.fromisoformat(f.read().strip())
    except FileNotFoundError:
        return None
    except ValueError as e:
        logger.warning(f"Ignoring unreadable {path}: {e}")
        return None


def write_date(path: str, day: date):
    with open(path, "w", encoding="utf-8") as f:
        f.write(day.isoformat())
