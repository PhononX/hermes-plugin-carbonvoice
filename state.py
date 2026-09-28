"""Cursor persistence for catch-up after Hermes restarts.

Two values are written to ``$HERMES_HOME/state/carbonvoice.json``:

- ``cursor`` — the opaque ``next_cursor`` of ``GET /v6/messages/updates``,
  the resume point of the poll feed. Preferred whenever present.
- ``lastSeenAt`` — an ISO timestamp, the ``date`` seed used to anchor the
  feed when there is no cursor yet (first run, or state written before the
  v6 migration) or when the server rejects the stored cursor.

Writes are debounced so a burst of messages doesn't fsync per message; on
shutdown the adapter calls ``stop()`` which forces a final flush.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from .constants import DEFAULT_FLUSH_DEBOUNCE_S

logger = logging.getLogger(__name__)


def default_state_path() -> Path:
    """Resolve ``$HERMES_HOME/state/carbonvoice.json``.

    Falls back to ``~/.hermes/state/carbonvoice.json`` if the Hermes
    constants module isn't importable (e.g. running outside the gateway).
    """
    try:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    except Exception:
        home = Path.home() / ".hermes"
    return home / "state" / "carbonvoice.json"


class Cursor:
    """Tracks the updates-feed ``cursor`` and the ``lastSeenAt`` date seed,
    with debounced disk persistence."""

    def __init__(self, path: Path, flush_debounce_s: float = DEFAULT_FLUSH_DEBOUNCE_S):
        self._path = path
        self._flush_debounce_s = flush_debounce_s
        self._last_seen_at: Optional[str] = None
        self._cursor: Optional[str] = None
        self._dirty = False
        self._flush_task: Optional[asyncio.Task] = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_seen_at(self) -> Optional[str]:
        return self._last_seen_at

    @property
    def cursor(self) -> Optional[str]:
        return self._cursor

    async def load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            last = data.get("lastSeenAt")
            if isinstance(last, str) and last:
                self._last_seen_at = last
            cursor = data.get("cursor")
            if isinstance(cursor, str) and cursor:
                self._cursor = cursor
            if self._cursor:
                logger.info("carbonvoice: resuming from stored updates cursor")
            elif self._last_seen_at:
                logger.info("carbonvoice: resuming from %s", self._last_seen_at)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("carbonvoice: failed to load state: %s", exc)

    def advance(self, iso_ts: str) -> None:
        """Move the ``date`` seed (used only when there is no cursor)."""
        self._last_seen_at = iso_ts
        self._dirty = True
        self._schedule_flush()

    def set_cursor(self, cursor: str, seed_iso: Optional[str] = None) -> None:
        """Store the feed's resume cursor, optionally refreshing the seed.

        The seed is what we fall back to if the server ever rejects this
        cursor, so the caller refreshes it on every fully-handled sync.
        """
        self._cursor = cursor
        if seed_iso:
            self._last_seen_at = seed_iso
        self._dirty = True
        self._schedule_flush()

    def clear_cursor(self) -> None:
        """Drop a cursor the server rejected; the next sync re-anchors by
        the ``lastSeenAt`` date seed."""
        if self._cursor is None:
            return
        self._cursor = None
        self._dirty = True
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            return

        async def _delayed():
            try:
                await asyncio.sleep(self._flush_debounce_s)
                await self.flush()
            except asyncio.CancelledError:
                pass

        self._flush_task = asyncio.create_task(_delayed())

    async def flush(self) -> None:
        if not self._dirty or (self._last_seen_at is None and self._cursor is None):
            return
        data = {"lastSeenAt": self._last_seen_at}
        if self._cursor:
            data["cursor"] = self._cursor
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data), encoding="utf-8")
            self._dirty = False
        except Exception as exc:
            logger.warning("carbonvoice: failed to flush state: %s", exc)

    async def stop(self) -> None:
        """Cancel any pending debounced flush, then force a final write."""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
        self._flush_task = None
        # Force-write whatever we have, even if the dirty flag was cleared
        # mid-flight — losing a cursor advance on shutdown is worse than a
        # redundant write.
        self._dirty = True
        await self.flush()
