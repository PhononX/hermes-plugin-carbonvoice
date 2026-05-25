"""In-memory cache for Carbon Voice channel metadata.

Maps ``channel_id`` → resolved ``chat_type`` ("dm" | "group") via
``GET /channel/{id}``. Channel kind almost never changes after creation
(a DM stays a DM forever) so we cache for the lifetime of the process;
the first message in a new channel pays one extra API call, every
message after is free.

A failed lookup is cached as ``"dm"`` so the adapter degrades to the
prior single-tier behavior rather than re-hitting the API on every
inbound message. If the channel is eventually created or becomes
accessible, a process restart picks up the correct type.
"""

from __future__ import annotations

import logging
from typing import Dict

from .api import CarbonVoiceAPI
from .parse import chat_type_from_channel

logger = logging.getLogger(__name__)


class ChannelCache:
    def __init__(self, api: CarbonVoiceAPI):
        self._api = api
        self._cache: Dict[str, str] = {}

    async def resolve_chat_type(self, channel_id: str) -> str:
        """Return ``"dm"`` or ``"group"`` for *channel_id*.

        Defaults to ``"dm"`` on any lookup failure so the agent keeps
        responding (previous behavior) rather than going silent because of
        a transient channel-API hiccup.
        """
        if not channel_id:
            return "dm"
        cached = self._cache.get(channel_id)
        if cached is not None:
            return cached
        try:
            data = await self._api.get_channel(channel_id)
        except Exception as exc:
            logger.debug(
                "carbonvoice: get_channel(%s) failed: %s", channel_id, exc
            )
            data = None
        chat_type = chat_type_from_channel(data)
        self._cache[channel_id] = chat_type
        return chat_type
