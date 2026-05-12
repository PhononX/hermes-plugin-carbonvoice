"""In-memory cache for Carbon Voice username resolution.

Maps ``user_guid`` → display name via ``GET /v3/users/{id}``. Names rarely
change so we cache for the lifetime of the process; the first message
from a new user pays one extra API call, every message after is free.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from .api import CarbonVoiceAPI

logger = logging.getLogger(__name__)


class UserCache:
    def __init__(self, api: CarbonVoiceAPI):
        self._api = api
        self._cache: Dict[str, str] = {}

    async def resolve(self, user_id: str) -> str:
        """Return the friendliest available name for *user_id*.

        Order of preference: ``display_name`` → ``username`` → ``name`` →
        the raw ``user_id`` itself (which is also stored as the fallback so
        we don't retry the lookup for unknown users every message).
        """
        if not user_id:
            return ""
        cached = self._cache.get(user_id)
        if cached is not None:
            return cached
        try:
            data = await self._api.get_user(user_id)
        except Exception as exc:
            logger.debug("carbonvoice: get_user(%s) failed: %s", user_id, exc)
            data = None
        name = self._pick_name(data) if data else user_id
        self._cache[user_id] = name
        return name

    @staticmethod
    def _pick_name(data: Dict[str, object]) -> Optional[str]:
        for key in ("display_name", "username", "name"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None
