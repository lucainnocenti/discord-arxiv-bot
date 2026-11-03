"""A best-effort lock so two synced machines don't run the bot at the same time.

The bot folder is shared between computers by a sync client, so both could
otherwise post the same papers and overwrite each other's state files. The
lock records which host is running; another host's lock is respected for a
limited time, so a crashed run never blocks the bot for good. Sync delays mean
this is not airtight, but it catches the common case of overlapping runs.
"""

import json
import logging
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

STALE_LOCK_AGE = timedelta(minutes=30)

logger = logging.getLogger("RunLock")


class LockHeldElsewhere(RuntimeError):
    pass


@contextmanager
def run_lock(path: str):
    """Holds the lock for the duration of the block, or raises LockHeldElsewhere."""
    host = socket.gethostname()
    existing = _read_lock(path)
    if existing and existing.get("host") != host:
        started = _parse_time(existing.get("started"))
        if started and datetime.now(timezone.utc) - started < STALE_LOCK_AGE:
            raise LockHeldElsewhere(
                f"Bot is already running on {existing.get('host')} (since {existing.get('started')})."
            )
        logger.warning(f"Ignoring stale run lock from {existing.get('host')} ({existing.get('started')}).")

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"host": host, "pid": os.getpid(), "started": datetime.now(timezone.utc).isoformat()}, f)
    try:
        yield
    finally:
        # Only remove our own lock, in case another host took over a stale one.
        if (_read_lock(path) or {}).get("host") == host:
            try:
                os.remove(path)
            except OSError as e:
                logger.warning(f"Could not remove run lock {path}: {e}")


def _read_lock(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
