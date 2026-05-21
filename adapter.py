"""Carbon Voice platform adapter for Hermes Agent.

Architecture:
    Hermes  <──Socket.IO (primary)──   api.carbonvoice.app
    Hermes  ── REST poll fallback ──>  /v3/messages/recent
    Hermes  ── POST /v3/messages/start ──>  outbound replies

This module is the thin orchestrator that wires together:

    parse        — payload-shape helpers (pure)
    api          — REST client (CarbonVoiceAPI)
    transport    — Socket.IO + polling lifecycle (Transport)
    state        — disk-persisted cursor (Cursor)
    dedupe       — in-memory seen-message TTL cache (SeenCache)
    reactions    — visual ack on inbound (ReactionService)
    users        — username resolution cache (UserCache)
    audit        — allowlist gate + ignored-sender audit log

No public webhook is required — the adapter holds an outbound Socket.IO
connection and polls /v3/messages/recent as a fallback. Cursor state is
persisted to disk so messages received while Hermes was offline are
processed on the next startup.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource

from .api import CarbonVoiceAPI
from .audit import AllowlistGate, IgnoredSenderLog, default_ignored_log_path
from .channels import ChannelCache
from .constants import (
    DEFAULT_BASE_URL,
    DEFAULT_POLL_INTERVAL_MS,
    DEFAULT_WS_RETRY_MAX_MS,
    MAX_MESSAGE_LENGTH,
)
from .dedupe import SeenCache
from .parse import (
    extract_channel_id,
    extract_creator_id,
    extract_message_id,
    extract_transcript,
    first_str,
    now_iso,
)
from .reactions import ReactionService
from .state import Cursor, default_state_path
from .transport import Transport
from .users import UserCache

logger = logging.getLogger(__name__)


class CarbonVoiceAdapter(BasePlatformAdapter):
    """Hermes ↔ Carbon Voice bridge over Socket.IO + REST polling fallback."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("carbonvoice"))
        extra = config.extra or {}
        pat: str = config.token or extra.get("pat") or ""
        base_url: str = (extra.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        poll_interval_s: float = (
            float(extra.get("poll_interval_ms") or DEFAULT_POLL_INTERVAL_MS) / 1000.0
        )
        ws_retry_max_s: float = (
            float(extra.get("ws_retry_max_ms") or DEFAULT_WS_RETRY_MAX_MS) / 1000.0
        )
        sp = extra.get("state_path")
        state_path: Path = Path(sp).expanduser() if sp else default_state_path()
        ilp = extra.get("ignored_senders_log")
        ignored_log_path: Path = (
            Path(ilp).expanduser() if ilp else default_ignored_log_path()
        )

        self._pat = pat
        self._creator_id: Optional[str] = extra.get("creator_id") or None
        self._self_user_id: Optional[str] = None
        self._mark_read_enabled: bool = not bool(extra.get("disable_mark_read"))

        self._api = CarbonVoiceAPI(pat, base_url) if pat and HTTPX_AVAILABLE else None
        self._cursor = Cursor(state_path)
        self._seen = SeenCache()
        self._transport = Transport(
            base_url=base_url,
            pat=pat,
            poll_interval_s=poll_interval_s,
            ws_retry_max_s=ws_retry_max_s,
            on_tick=self._fetch_missed_messages,
        )
        self._users = UserCache(self._api) if self._api else None
        self._channels = ChannelCache(self._api) if self._api else None
        self._reactions = (
            ReactionService(
                self._api,
                reaction_id=extra.get("reaction_id"),
                enabled=not bool(extra.get("disable_ack_reaction")),
            )
            if self._api
            else None
        )
        self._allowlist = AllowlistGate.from_env()
        self._ignored_log = (
            IgnoredSenderLog(ignored_log_path, self._users) if self._users else None
        )

        # Maps channel_id → "reply anchor" (parent_message_id or top-level
        # message_id). Used to thread our outbound replies under the latest
        # inbound message. We store the anchor rather than the raw message_id
        # because Carbon Voice rejects reply_to pointing at a reply.
        self._last_inbound_msg: Dict[str, str] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        if not self._pat or self._api is None:
            logger.error("carbonvoice: CARBONVOICE_PAT not set")
            return False

        await self._api.open()

        try:
            self._self_user_id = await self._api.whoami()
        except Exception as exc:
            logger.error("carbonvoice: /whoami failed: %s", exc)
            await self._api.close()
            return False
        if not self._self_user_id:
            logger.error("carbonvoice: /whoami returned no user id")
            await self._api.close()
            return False

        if self._reactions is not None:
            await self._reactions.discover()

        await self._cursor.load()

        try:
            await self._fetch_missed_messages()
        except Exception as exc:
            logger.warning("carbonvoice: initial catch-up failed: %s", exc)

        await self._transport.start()

        self._mark_connected()
        logger.info(
            "carbonvoice: connected as %s (mode=%s, state=%s)",
            self._self_user_id, self._transport.mode, self._cursor.path,
        )
        return True

    async def disconnect(self) -> None:
        await self._transport.stop()
        await self._cursor.stop()
        if self._api is not None:
            await self._api.close()
        self._mark_disconnected()

    # ── Outbound (Hermes → Carbon Voice) ─────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if self._api is None:
            return SendResult(success=False, error="adapter not connected")
        if not content or not content.strip():
            return SendResult(success=False, error="empty content")

        reply_target = reply_to or self._last_inbound_msg.get(chat_id)

        try:
            data = await self._api.send_message(chat_id, content, reply_to=reply_target)
            msg_id = first_str(data.get("message_id"), data.get("id"))
            return SendResult(success=True, message_id=msg_id, raw_response=data)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            body = exc.response.text if exc.response is not None else ""

            # Safety net: if CV rejects our reply_to because the target is
            # itself a reply, drop the anchor and retry as a top-level
            # message. Covers stale state from before the lane-anchor fix
            # and any future drift between our cache and CV's tree.
            if (
                status == 400
                and reply_target
                and "cannot reply to a message that is a reply" in body.lower()
            ):
                logger.warning(
                    "carbonvoice: stale reply anchor %s — retrying as top-level",
                    reply_target,
                )
                if self._last_inbound_msg.get(chat_id) == reply_target:
                    self._last_inbound_msg.pop(chat_id, None)
                try:
                    data = await self._api.send_message(chat_id, content, reply_to=None)
                    msg_id = first_str(data.get("message_id"), data.get("id"))
                    return SendResult(success=True, message_id=msg_id, raw_response=data)
                except Exception as exc2:
                    return SendResult(success=False, error=f"top-level retry failed: {exc2}")

            return SendResult(
                success=False,
                error=f"HTTP {status}: {body[:500]}",
                retryable=status in (408, 429, 500, 502, 503, 504),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "carbonvoice", "chat_id": chat_id}

    # ── Inbound processing ───────────────────────────────────────────────

    async def _fetch_missed_messages(self) -> None:
        if self._api is None:
            return

        request_started_at = now_iso()

        if not self._cursor.last_seen_at:
            logger.info(
                "carbonvoice: first run, starting from %s", request_started_at
            )
            self._cursor.advance(request_started_at)
            return

        try:
            messages = await self._api.fetch_recent(self._cursor.last_seen_at)
        except Exception as exc:
            logger.warning("carbonvoice: /v3/messages/recent failed: %s", exc)
            return  # don't advance cursor — retry same window next tick

        messages.sort(key=lambda m: m.get("created_at") or "")

        for msg in messages:
            try:
                await self._process_message(msg)
            except Exception as exc:
                logger.error("carbonvoice: process_message error: %s", exc)

        # Advance cursor to when this request was fired, not message timestamps —
        # avoids missing concurrent writes that landed during the call.
        self._cursor.advance(request_started_at)

    async def _process_message(self, msg: Dict[str, Any]) -> bool:
        message_id = extract_message_id(msg)
        if not message_id:
            return False

        channel_id = extract_channel_id(msg)
        if not channel_id:
            return False

        creator_id = extract_creator_id(msg)

        # Self-loop guard.
        if creator_id and self._self_user_id and creator_id == self._self_user_id:
            return False

        # Optional single-user restriction (acts before transcript check so
        # we don't waste cycles on transcripts we'll drop anyway).
        if self._creator_id and creator_id and creator_id != self._creator_id:
            return False

        # Allowlist gate — default is allow-all (see AllowlistGate docstring).
        # When the operator has configured a restriction, short-circuit
        # rejected senders here so we can log them with a resolved username
        # before Hermes core's parallel check drops them.
        if not self._allowlist.is_allowed(creator_id):
            logger.info(
                "carbonvoice: dropped message from unauthorized sender %s",
                creator_id,
            )
            if self._ignored_log is not None and creator_id:
                self._ignored_log.record(creator_id, channel_id)
            return False

        # Dedupe early so retries on still-transcribing messages don't multiply.
        if self._seen.is_seen(message_id):
            return False

        # Two-phase transcript: empty means "not ready yet" — don't mark seen.
        transcript = extract_transcript(msg)
        if not transcript:
            return False

        self._seen.mark(message_id)

        # Fire the visual ack first so the user sees feedback in <100ms,
        # well before the agent's reply (which can take 10+ seconds).
        if self._reactions is not None:
            self._reactions.ack(message_id)

        # Lane anchor: ONLY top-level messages (no parent) define the
        # thread we'll reply under. Carbon Voice's `parent_message_id` is
        # the direct parent in the tree, not the lane anchor — so if an
        # inbound reply's parent is itself a Hermes reply, threading our
        # next response under that parent gets us a CV 400 ("cannot reply
        # to a message that is a reply"). Keep whatever anchor we had;
        # the next top-level message overwrites it.
        parent = first_str(
            msg.get("parent_message_id"), msg.get("parent_message_guid")
        )
        if parent is None:
            self._last_inbound_msg[channel_id] = message_id

        user_name = ""
        if creator_id and self._users is not None:
            user_name = await self._users.resolve(creator_id)
        elif creator_id:
            user_name = creator_id

        chat_type = "dm"
        if self._channels is not None:
            chat_type = await self._channels.resolve_chat_type(channel_id)

        reply_to_text = await self._resolve_parent_text(parent)

        source = SessionSource(
            platform=Platform("carbonvoice"),
            chat_id=channel_id,
            chat_name=f"cv:{channel_id[:8]}",
            chat_type=chat_type,
            user_id=creator_id or "",
            user_name=user_name or creator_id or "",
            message_id=message_id,
        )
        event = MessageEvent(
            text=transcript,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=msg,
            message_id=message_id,
            reply_to_message_id=parent,
            reply_to_text=reply_to_text,
        )

        # Dispatch in a background task so processing one message can't block
        # the poll/WS loop while the agent thinks.
        asyncio.create_task(self._dispatch(event))
        return True

    async def _resolve_parent_text(self, parent_id: Optional[str]) -> Optional[str]:
        """Fetch a parent message's transcript so the agent sees thread context.

        Returns ``None`` on a missing id, a failed lookup, or an empty
        transcript — Hermes treats ``None`` as "no reply context", which
        matches the prior behavior. Failures are logged at debug so a flaky
        parent endpoint does not spam the operator's logs.
        """
        if not parent_id or self._api is None:
            return None
        try:
            parent_msg = await self._api.get_message(parent_id)
        except Exception as exc:
            logger.debug(
                "carbonvoice: get_message(%s) failed: %s", parent_id, exc
            )
            return None
        if not parent_msg:
            return None
        text = extract_transcript(parent_msg)
        return text or None

    async def _dispatch(self, event: MessageEvent) -> None:
        try:
            await self.handle_message(event)
        except Exception as exc:
            logger.exception("carbonvoice: dispatch failed: %s", exc)
        finally:
            # Clear the unread badge once we've at least attempted handling.
            # On failure we still mark read — the operator sees the error in
            # logs; leaving the notification doesn't trigger a retry.
            if self._mark_read_enabled and self._api is not None:
                channel_id = event.source.chat_id
                msg_id = event.message_id
                if channel_id and msg_id:
                    try:
                        await self._api.mark_read(channel_id, msg_id)
                    except Exception as exc:
                        logger.debug(
                            "carbonvoice: mark_read(%s, %s) failed: %s",
                            channel_id, msg_id, exc,
                        )
