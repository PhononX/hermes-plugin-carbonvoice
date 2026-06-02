"""In-memory cache for Carbon Voice channel metadata.

One ``GET /channel/{id}`` per channel populates two things, both keyed by
``channel_id`` and cached for the process lifetime:

  - **chat_type** ("dm" | "group") — channel kind almost never changes
    after creation (a DM stays a DM forever).
  - **roster** (``{user_guid → display name}``) — derived from the
    channel's ``json_collaborators``. This is the canonical way to
    resolve participant names: the standalone ``GET /v3/users/{id}``
    endpoint is dead (404), and the collaborator list rides on the same
    payload we already fetch for chat-type, so names cost zero extra
    calls.

The first message in a new channel pays one API call; every message
after is free for both axes. A failed lookup caches ``"dm"`` + an empty
roster so the adapter degrades gracefully (keeps responding, falls back
to the raw guid for names) rather than re-hitting the API per message.

Roster note: membership *can* change (people join/leave), but names
rarely do; we accept process-lifetime caching for v1. A restart refreshes
both. If stale rosters ever bite, add a TTL here with the same shape.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from .api import CarbonVoiceAPI
from .parse import chat_type_from_channel, extract_roster

logger = logging.getLogger(__name__)


class ChannelCache:
    def __init__(self, api: CarbonVoiceAPI):
        self._api = api
        self._type_cache: Dict[str, str] = {}
        self._roster_cache: Dict[str, Dict[str, str]] = {}

    async def _ensure_loaded(self, channel_id: str) -> None:
        """Fetch the channel once and populate both caches."""
        if channel_id in self._type_cache:
            return
        try:
            data = await self._api.get_channel(channel_id)
        except Exception as exc:
            logger.debug(
                "carbonvoice: get_channel(%s) failed: %s", channel_id, exc
            )
            data = None
        self._type_cache[channel_id] = chat_type_from_channel(data)
        self._roster_cache[channel_id] = extract_roster(data)

    async def resolve_chat_type(self, channel_id: str) -> str:
        """Return ``"dm"`` or ``"group"`` for *channel_id*.

        Defaults to ``"dm"`` on any lookup failure so the agent keeps
        responding (previous behavior) rather than going silent because of
        a transient channel-API hiccup.
        """
        if not channel_id:
            return "dm"
        await self._ensure_loaded(channel_id)
        return self._type_cache.get(channel_id, "dm")

    async def get_roster(self, channel_id: str) -> Dict[str, str]:
        """Return ``{user_guid → display name}`` for *channel_id*.

        Empty dict on lookup failure. Shares the cached channel payload
        with :meth:`resolve_chat_type`, so calling both for one message is
        a single HTTP call.
        """
        if not channel_id:
            return {}
        await self._ensure_loaded(channel_id)
        return self._roster_cache.get(channel_id, {})

    async def resolve_name(
        self, channel_id: str, user_guid: str
    ) -> Optional[str]:
        """Display name for *user_guid* in *channel_id*, or ``None``.

        ``None`` means "not in this channel's collaborator list" — callers
        fall back to the raw guid.
        """
        if not channel_id or not user_guid:
            return None
        roster = await self.get_roster(channel_id)
        return roster.get(user_guid)
