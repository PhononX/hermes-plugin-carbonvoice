"""Allowlist gating + ignored-sender audit log.

Hermes core already enforces ``CARBONVOICE_ALLOWED_USERS`` /
``CARBONVOICE_ALLOW_ALL_USERS`` *after* the adapter dispatches. We
replicate the check inside the adapter so we can:

  1. Short-circuit before the agent ever sees the message (cheaper).
  2. Record the rejection in an append-only audit log with the resolved
     username, so the operator can see who's trying to reach the bot.

Log path defaults to ``$HERMES_HOME/logs/carbonvoice-ignored-senders.log``
and is one JSON object per line: ``{"time", "user_id", "username", "channel_id"}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set

from .users import UserCache

logger = logging.getLogger(__name__)


class AllowlistGate:
    """Mirrors Hermes' allowlist semantics so we can log rejections."""

    def __init__(self, allow_all: bool, allowed_ids: Set[str]):
        self._allow_all = allow_all
        self._allowed_ids = allowed_ids

    @classmethod
    def from_env(cls) -> "AllowlistGate":
        allow_all = os.getenv("CARBONVOICE_ALLOW_ALL_USERS", "").strip().lower() in (
            "true", "1", "yes", "on",
        )
        raw = os.getenv("CARBONVOICE_ALLOWED_USERS", "")
        allowed = {u.strip() for u in raw.split(",") if u.strip()}
        return cls(allow_all=allow_all, allowed_ids=allowed)

    def is_allowed(self, user_id: Optional[str]) -> bool:
        if self._allow_all:
            return True
        if not user_id:
            return False
        return user_id in self._allowed_ids

    @property
    def is_inactive(self) -> bool:
        """True when no gating env vars are set — defer to Hermes core's filter."""
        return not self._allow_all and not self._allowed_ids


class IgnoredSenderLog:
    """Append-only JSON-lines log of rejected inbound senders."""

    def __init__(self, path: Path, users: UserCache):
        self._path = path
        self._users = users

    @property
    def path(self) -> Path:
        return self._path

    def record(self, user_id: str, channel_id: Optional[str] = None) -> None:
        """Fire-and-forget — never blocks the inbound path."""
        asyncio.create_task(self._record(user_id, channel_id))

    async def _record(self, user_id: str, channel_id: Optional[str]) -> None:
        try:
            username = await self._users.resolve(user_id) if user_id else ""
            entry = {
                "time": datetime.now(timezone.utc).isoformat(),
                "user_id": user_id,
                "username": username,
            }
            if channel_id:
                entry["channel_id"] = channel_id
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.debug("carbonvoice: ignored-sender log failed: %s", exc)


def default_ignored_log_path() -> Path:
    """``$HERMES_HOME/logs/carbonvoice-ignored-senders.log`` with safe fallback."""
    try:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    except Exception:
        home = Path.home() / ".hermes"
    return home / "logs" / "carbonvoice-ignored-senders.log"
