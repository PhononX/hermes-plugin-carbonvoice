"""Thin async wrapper around the Carbon Voice REST endpoints we use.

Methods raise on HTTP/network errors so callers can map them to their own
result types (the adapter wraps them into ``SendResult``; ``standalone_send``
catches everything and returns a dict).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from .constants import DEFAULT_BASE_URL, HTTP_TIMEOUT
from .parse import auth_headers, first_str

logger = logging.getLogger(__name__)


class CarbonVoiceAPI:
    """Stateless REST client. Open with ``await api.open()`` before use."""

    def __init__(self, pat: str, base_url: str = DEFAULT_BASE_URL):
        if not HTTPX_AVAILABLE:
            raise RuntimeError("httpx not installed")
        self._pat = pat
        self._base_url = base_url.rstrip("/")
        self._client: Optional["httpx.AsyncClient"] = None

    @property
    def base_url(self) -> str:
        return self._base_url

    async def open(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=auth_headers(self._pat),
                timeout=HTTP_TIMEOUT,
            )

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    def _require_client(self) -> "httpx.AsyncClient":
        if self._client is None:
            raise RuntimeError("CarbonVoiceAPI used before open()")
        return self._client

    async def whoami(self) -> Optional[str]:
        """Return the agent's own ``user_guid``, or None when not parseable."""
        client = self._require_client()
        resp = await client.get("/whoami")
        resp.raise_for_status()
        data = resp.json() or {}
        user = data.get("user") or {}
        return first_str(user.get("user_guid"), user.get("_id"), user.get("id"))

    async def fetch_recent(
        self,
        since_iso: str,
        direction: str = "newer",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        client = self._require_client()
        body = {
            "date": since_iso,
            "direction": direction,
            "limit": limit,
            "use_last_updated": False,
        }
        resp = await client.post("/v3/messages/recent", json=body)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v3/messages/start. Returns the parsed response dict on 2xx."""
        client = self._require_client()
        body: Dict[str, Any] = {
            "unique_client_id": str(uuid.uuid4()),
            "transcript": content,
            "is_text_message": True,
            "is_streaming": False,
            "channel_id": channel_id.strip(),
        }
        if reply_to:
            body["reply_to_message_id"] = str(reply_to)
        resp = await client.post("/v3/messages/start", json=body)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def fetch_reactions(self) -> List[Dict[str, Any]]:
        """GET /reactions — returns the workspace's available reactions."""
        client = self._require_client()
        resp = await client.get("/reactions")
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def react(self, reaction_id: str, message_id: str) -> None:
        """POST /reactions/{reaction_id}/{message_id} — empty body."""
        client = self._require_client()
        resp = await client.post(f"/reactions/{reaction_id}/{message_id}")
        resp.raise_for_status()

    async def mark_read(self, channel_id: str, message_id: str) -> None:
        """DELETE /notifications/{channel}/{message} — clears the unread badge."""
        client = self._require_client()
        resp = await client.delete(
            f"/notifications/{channel_id}/{message_id}",
            params={"type": "message", "notification_removal_mode": "hard"},
        )
        resp.raise_for_status()

    async def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """GET /v3/users/{user_id} — returns user profile dict or None on 4xx."""
        client = self._require_client()
        resp = await client.get(f"/v3/users/{user_id}")
        if resp.status_code >= 400:
            return None
        return resp.json() if resp.content else None


async def standalone_send(
    pat: str,
    base_url: str,
    channel_id: str,
    content: str,
) -> Dict[str, Any]:
    """One-shot send for out-of-process delivery (cron). No persistent client."""
    if not HTTPX_AVAILABLE:
        return {"success": False, "error": "httpx not installed"}
    body = {
        "unique_client_id": str(uuid.uuid4()),
        "transcript": content,
        "is_text_message": True,
        "is_streaming": False,
        "channel_id": channel_id.strip(),
    }
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers=auth_headers(pat),
        timeout=HTTP_TIMEOUT,
    ) as client:
        try:
            resp = await client.post("/v3/messages/start", json=body)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            return {
                "success": True,
                "message_id": first_str(data.get("message_id"), data.get("id")),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}
