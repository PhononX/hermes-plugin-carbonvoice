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
        """POST /v3/messages/start — legacy. Prefer ``send_text_v5``.

        Kept for compatibility with code paths that still pass through the v3
        contract. New code should use ``send_text_v5`` which accepts
        ``thread_id`` directly (no reply-anchor resolution required) and
        uses ``idempotency_key`` instead of the deprecated
        ``unique_client_id``.
        """
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

    # ── v5 transport ────────────────────────────────────────────────────
    #
    # The v5 endpoints replace the v3 contract with cleaner naming
    # (``thread_id`` as the preferred public field, ``idempotency_key``
    # in place of ``unique_client_id``) and split create paths by media
    # kind: ``/text``, ``/audio`` (multipart), and ``/attachment`` (URLs).
    #
    # The CV team's design intent — "Just always reply to ``thread_id``
    # when wanting to do thread; eliminate client guessing" — is encoded
    # natively in these endpoints. Callers pass ``thread_id`` from the
    # inbound message (``ConversationTracker.thread_id_of(msg)``) and the
    # server resolves the rest. No reply-anchor lookup needed.

    async def send_text_v5(
        self,
        conversation_id: str,
        transcript: str,
        thread_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v5/messages/text — create a text message in a conversation.

        Returns the created MessageV5 dict on 2xx. ``thread_id`` is the
        preferred field for threading (see module docstring); pass
        ``None`` for a new top-level post.
        """
        client = self._require_client()
        body: Dict[str, Any] = {
            "conversation_id": conversation_id.strip(),
            "transcript": transcript,
            "idempotency_key": idempotency_key or str(uuid.uuid4()),
        }
        if thread_id:
            body["thread_id"] = str(thread_id)
        resp = await client.post("/v5/messages/text", json=body)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def send_audio_v5(
        self,
        conversation_id: str,
        audio_path: str,
        thread_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /v5/messages/audio — multipart upload of an audio file.

        Sends two parts: ``payload`` (JSON with conversation_id /
        thread_id / idempotency_key / duration) and ``audio_file`` (the
        raw bytes of the file at ``audio_path``). The server transcribes
        and threads the resulting message; returns the created
        MessageV5 dict on 2xx.

        For Hermes' ``send_voice`` adapter override.
        """
        import json as _json
        from pathlib import Path as _Path

        client = self._require_client()
        path = _Path(audio_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"audio file not found: {path}")

        payload: Dict[str, Any] = {
            "conversation_id": conversation_id.strip(),
            "idempotency_key": idempotency_key or str(uuid.uuid4()),
        }
        if thread_id:
            payload["thread_id"] = str(thread_id)
        if duration_ms is not None:
            payload["duration"] = int(duration_ms)

        with path.open("rb") as fh:
            files = {
                "payload": (None, _json.dumps(payload), "application/json"),
                "audio_file": (path.name, fh.read(), "application/octet-stream"),
            }
            resp = await client.post("/v5/messages/audio", files=files)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def send_attachment_v5(
        self,
        conversation_id: str,
        attachments: List[Dict[str, Any]],
        thread_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v5/messages/attachment — create a message with link attachments.

        ``attachments`` is a list of ``V5RequestAttachmentPayload`` dicts
        (``{type, link, idempotency_key?, ...}``). The CV API expects
        each attachment to reference an already-hosted resource by URL;
        binary uploads via this endpoint are not supported (use
        ``send_audio_v5`` for audio, or host the file elsewhere first and
        pass the URL here).

        For Hermes' ``send_image`` / ``send_document`` adapter overrides
        when the caller passes a URL.
        """
        client = self._require_client()
        body: Dict[str, Any] = {
            "conversation_id": conversation_id.strip(),
            "attachments": attachments,
            "idempotency_key": idempotency_key or str(uuid.uuid4()),
        }
        if thread_id:
            body["thread_id"] = str(thread_id)
        resp = await client.post("/v5/messages/attachment", json=body)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def get_message_v5(self, message_id: str) -> Optional[Dict[str, Any]]:
        """GET /v5/messages/{id} — returns the MessageV5 dict or None on 4xx.

        Same shape as ``GET /v3/messages/{id}`` but with ``thread_id`` as
        the preferred public field and ``parent_message_id`` as the
        deprecated alias. Used by future memory wiring to fetch full
        thread context on @mention without local buffering.
        """
        client = self._require_client()
        resp = await client.get(f"/v5/messages/{message_id}")
        if resp.status_code >= 400:
            return None
        return resp.json() if resp.content else None

    async def get_messages_by_ids_v5(
        self, message_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """POST /v5/messages/by-ids — batch fetch of multiple MessageV5s.

        Used by future memory wiring to fetch all replies in a thread
        efficiently after resolving the parent ids from a single
        ``get_message_v5`` call.
        """
        if not message_ids:
            return []
        client = self._require_client()
        resp = await client.post("/v5/messages/by-ids", json={"ids": message_ids})
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("messages", []) if isinstance(data, dict) else []

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

    async def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        """GET /v3/messages/{message_id} — returns the message dict or None on 4xx.

        Same payload shape as inbound Socket.IO / fetch_recent messages, so
        parse helpers (``extract_transcript``, ``extract_creator_id``, etc.)
        work unchanged. Used to resolve the text of a parent message when an
        inbound reply carries ``parent_message_id`` — gives the agent the
        thread context it would otherwise have to guess at.
        """
        client = self._require_client()
        resp = await client.get(f"/v3/messages/{message_id}")
        if resp.status_code >= 400:
            return None
        return resp.json() if resp.content else None

    async def get_channel(self, channel_id: str) -> Optional[Dict[str, Any]]:
        """GET /channel/{id} — returns the PersonalizedChannel dict or None on 4xx.

        Carbon Voice's response exposes ``type`` (directMessage |
        customerConversation | namedConversation | asyncMeeting) and
        ``dm_hash`` (null for non-DMs) — both usable to discriminate DM
        vs group conversation when gating the agent's behavior.
        """
        client = self._require_client()
        resp = await client.get(f"/channel/{channel_id}")
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
