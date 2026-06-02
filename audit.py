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
from typing import TYPE_CHECKING, Optional, Set

if TYPE_CHECKING:
    from .channels import ChannelCache

logger = logging.getLogger(__name__)


class AllowlistGate:
    """Default-allow access control for inbound Carbon Voice messages.

    Semantics (intentionally permissive — the plugin is mostly used for
    personal/dev bots where install-and-forget should "just work"):

    - ``CARBONVOICE_ALLOWED_USERS=a,b,c`` → only those user_guids accepted
    - ``CARBONVOICE_ALLOW_ALL_USERS=false`` (with no allowed list) → deny all
    - Anything else (including unset) → allow all

    Setting ``CARBONVOICE_ALLOW_ALL_USERS=true`` is redundant but accepted
    for explicitness; the in-process default in ``register()`` writes it
    to ``os.environ`` so Hermes core's own allowlist check also sees it.
    """

    def __init__(self, allow_all: bool, allowed_ids: Set[str]):
        self._allow_all = allow_all
        self._allowed_ids = allowed_ids

    @classmethod
    def from_env(cls) -> "AllowlistGate":
        raw = os.getenv("CARBONVOICE_ALLOWED_USERS", "")
        allowed = {u.strip() for u in raw.split(",") if u.strip()}
        if allowed:
            return cls(allow_all=False, allowed_ids=allowed)
        # No specific list — default open, unless explicitly disabled.
        disabled = os.getenv("CARBONVOICE_ALLOW_ALL_USERS", "").strip().lower() in (
            "false", "0", "no", "off",
        )
        return cls(allow_all=not disabled, allowed_ids=set())

    def is_allowed(self, user_id: Optional[str]) -> bool:
        if self._allow_all:
            return True
        if not user_id:
            return False
        return user_id in self._allowed_ids


class IgnoredSenderLog:
    """Append-only JSON-lines log of rejected inbound senders."""

    def __init__(self, path: Path, channels: "ChannelCache"):
        self._path = path
        self._channels = channels

    @property
    def path(self) -> Path:
        return self._path

    def record(self, user_id: str, channel_id: Optional[str] = None) -> None:
        """Fire-and-forget — never blocks the inbound path."""
        asyncio.create_task(self._record(user_id, channel_id))

    async def _record(self, user_id: str, channel_id: Optional[str]) -> None:
        try:
            # Resolve the name from the channel roster when we have a
            # channel; an unauthorized sender may not be a collaborator,
            # in which case this is None and we log just the guid.
            username = ""
            if user_id and channel_id:
                username = (
                    await self._channels.resolve_name(channel_id, user_id) or ""
                )
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
