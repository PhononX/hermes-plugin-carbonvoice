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
from .conversations import ConversationTracker
from .constants import (
    DEFAULT_BASE_URL,
    DEFAULT_POLL_INTERVAL_MS,
    DEFAULT_WS_RETRY_MAX_MS,
    MAX_MESSAGE_LENGTH,
)
from .dedupe import SeenCache
from .gate import MentionGate
from .parse import (
    extract_channel_id,
    extract_creator_id,
    extract_message_id,
    extract_transcript,
    first_str,
    now_iso,
    strip_inline_mentions,
)
# extract_transcript is also re-exported via parse for the parent-text path
# (now handled by ConversationTracker, but the import here is kept so
# extract_transcript stays available for any future inline use).
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
        self._gate = MentionGate.from_env()
        self._ignored_log = (
            IgnoredSenderLog(ignored_log_path, self._users) if self._users else None
        )

        # Per-thread reply anchors + parent-text cache + (eventually)
        # engagement / outbound tracking. See conversations.py and
        # DEVELOPMENT.md §7.5 for the design.
        self._tracker = ConversationTracker(self._api)

        # Transitional ``chat_id → most-recent thread_id`` index used by
        # ``send()`` to look up the right reply anchor while no caller has
        # ``metadata['thread_id']`` populated. PR 2 wires
        # ``SessionSource.thread_id`` and Hermes core will then pass
        # ``thread_id`` through ``metadata`` (see
        # ``gateway/platforms/base.py::_thread_metadata_for_source``); at
        # that point this index can be dropped. Keeping it preserves the
        # exact pre-tracker behavior for outbound threading while the
        # tracker already stores per-thread anchors correctly — the
        # latent ``§7.6`` bug is structurally fixed (anchors no longer
        # trample), but the *read path* still resolves via channel until
        # PR 2 closes the loop.
        self._chat_thread_index: Dict[str, str] = {}

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

        # Resolve the reply anchor. Preference order:
        #   1. Explicit ``reply_to`` from the caller (rare).
        #   2. ``metadata['thread_id']`` — populated by Hermes core once
        #      PR 2 wires ``SessionSource.thread_id`` (see
        #      ``gateway/platforms/base.py::_thread_metadata_for_source``).
        #   3. ``_chat_thread_index[chat_id]`` — the transitional
        #      most-recent-thread-per-channel index that preserves the
        #      pre-tracker single-thread-per-channel behavior. Drops out
        #      in PR 2 when (2) is reliable.
        reply_target = reply_to
        if not reply_target:
            thread_hint = (metadata or {}).get("thread_id") or self._chat_thread_index.get(chat_id)
            if thread_hint:
                reply_target = self._tracker.get_reply_anchor(thread_hint)

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
                # Clear any tracker entries pointing at the stale anchor and
                # drop the channel index if it was the trigger.
                self._tracker.clear_reply_anchor(reply_target)
                if self._chat_thread_index.get(chat_id) == reply_target:
                    self._chat_thread_index.pop(chat_id, None)
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

        # Resolve chat_type before the mention gate so the gate can short-
        # circuit group messages without spinning up the rest of the
        # pipeline (visual ack, parent lookup, name resolution). The
        # channel cache makes the first message in each channel pay one
        # HTTP call; every subsequent message is free.
        chat_type = "dm"
        if self._channels is not None:
            chat_type = await self._channels.resolve_chat_type(channel_id)

        # Mention gate: in group channels, only respond when the agent
        # is @-mentioned (or the channel is configured to bypass). DMs
        # always pass. Evaluated before the visual ack so users in
        # non-mention scenarios don't see a phantom "I saw it" with no
        # follow-up reply.
        decision = self._gate.evaluate(
            msg=msg,
            chat_type=chat_type,
            channel_id=channel_id,
            self_user_id=self._self_user_id,
        )
        if not decision.process:
            logger.debug(
                "carbonvoice: skip message %s in %s — %s",
                message_id, channel_id, decision.reason,
            )
            return False

        # Fire the visual ack first so the user sees feedback in <100ms,
        # well before the agent's reply (which can take 10+ seconds).
        if self._reactions is not None:
            self._reactions.ack(message_id)

        # Lane anchor: compute the thread root for this inbound message
        # and record it in the tracker so the next outbound reply threads
        # under the correct root. Carbon Voice enforces flat replies (see
        # DEVELOPMENT.md §4), so ``parent_message_id`` is always the true
        # root — no walking required. The tracker stores the anchor keyed
        # by ``thread_id`` (fixes the latent §7.6 bug: concurrent threads
        # in the same channel no longer trample each other's anchor at
        # the storage layer). The ``_chat_thread_index`` write preserves
        # the read-side behavior until PR 2 wires ``metadata['thread_id']``
        # into ``send()`` — see the index docstring.
        parent = first_str(
            msg.get("parent_message_id"), msg.get("parent_message_guid")
        )
        thread_id = ConversationTracker.thread_id_of(msg)
        if thread_id:
            self._tracker.set_reply_anchor(thread_id, thread_id)
            self._chat_thread_index[channel_id] = thread_id

        user_name = ""
        if creator_id and self._users is not None:
            user_name = await self._users.resolve(creator_id)
        elif creator_id:
            user_name = creator_id

        reply_to_text = await self._tracker.get_parent_text(parent)

        # Strip CV's inline @[name](guid) markup so the agent sees
        # readable text — the guid in the original is LLM noise that
        # can confuse instruction following.
        clean_text = strip_inline_mentions(transcript)

        # Session sharing in groups: pass the thread root as
        # ``SessionSource.thread_id`` so Hermes core composes a shared
        # session key (``agent:main:carbonvoice:group:<chat_id>:<thread_id>``)
        # and prefixes each user message with ``[sender name]`` for
        # multi-user attribution. DMs intentionally keep ``thread_id=None``:
        # a DM should remain one session per pair, not split per top-level
        # message inside the conversation.
        session_thread_id = thread_id if chat_type == "group" else None

        source = SessionSource(
            platform=Platform("carbonvoice"),
            chat_id=channel_id,
            chat_name=f"cv:{channel_id[:8]}",
            chat_type=chat_type,
            user_id=creator_id or "",
            user_name=user_name or creator_id or "",
            message_id=message_id,
            thread_id=session_thread_id,
        )
        event = MessageEvent(
            text=clean_text,
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
