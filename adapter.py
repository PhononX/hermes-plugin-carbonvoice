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

        # v5 transport: pass ``thread_id`` directly to the server. The CV
        # team's design intent — "Just always reply to thread_id when
        # wanting to do thread; eliminate client guessing" — means the
        # server resolves the threading; no client-side reply-anchor
        # lookup is required.
        #
        # ``thread_id`` priority:
        #   1. ``metadata['thread_id']`` — populated by Hermes core from
        #      ``SessionSource.thread_id`` for group messages.
        #   2. ``reply_to`` from the caller — used as a fallback when no
        #      thread context exists (DMs keep thread_id=None on
        #      ``SessionSource`` to preserve one-session-per-DM-pair).
        thread_id = (metadata or {}).get("thread_id") or reply_to

        try:
            data = await self._api.send_text_v5(
                conversation_id=chat_id,
                transcript=content,
                thread_id=thread_id,
            )
            msg_id = first_str(data.get("id"), data.get("message_id"))
            return SendResult(success=True, message_id=msg_id, raw_response=data)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            body = exc.response.text if exc.response is not None else ""
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

    async def send_voice(
        self,
        chat_id: str,
        path: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a voice memo via ``POST /v5/messages/audio`` (multipart).

        ``path`` is a local audio file. CV transcribes it server-side and
        threads the resulting message using ``thread_id`` from metadata
        (same resolution rules as :meth:`send`).
        """
        if self._api is None:
            return SendResult(success=False, error="adapter not connected")
        thread_id = (metadata or {}).get("thread_id") or reply_to
        try:
            data = await self._api.send_audio_v5(
                conversation_id=chat_id,
                audio_path=path,
                thread_id=thread_id,
            )
            msg_id = first_str(data.get("id"), data.get("message_id"))
            return SendResult(success=True, message_id=msg_id, raw_response=data)
        except FileNotFoundError as exc:
            return SendResult(success=False, error=str(exc))
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            body = exc.response.text if exc.response is not None else ""
            return SendResult(
                success=False,
                error=f"HTTP {status}: {body[:500]}",
                retryable=status in (408, 429, 500, 502, 503, 504),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image attachment via ``POST /v5/messages/attachment``.

        ``image_url`` must be a publicly-resolvable URL (the v5 attachment
        endpoint is URL-based; binary uploads aren't supported here).
        ``caption``, if provided, becomes the message transcript via a
        follow-up :meth:`send` so the agent's text accompanies the image.

        Local file paths are not supported in this v1 — callers needing
        local-file uploads should use :meth:`send_image_file` (currently
        unimplemented; see :class:`BasePlatformAdapter` for the default
        no-op).
        """
        return await self._send_attachment(
            chat_id=chat_id,
            url=image_url,
            attachment_type="image",
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_document(
        self,
        chat_id: str,
        path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a document attachment via ``POST /v5/messages/attachment``.

        ``path`` must be a publicly-resolvable URL (the v5 attachment
        endpoint is URL-based). Local file paths are not supported in
        this v1 — see :meth:`send_image` for the same rationale.
        """
        return await self._send_attachment(
            chat_id=chat_id,
            url=path,
            attachment_type="document",
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def _send_attachment(
        self,
        *,
        chat_id: str,
        url: str,
        attachment_type: str,
        caption: Optional[str],
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
    ) -> SendResult:
        if self._api is None:
            return SendResult(success=False, error="adapter not connected")
        if not url:
            return SendResult(success=False, error="attachment URL required")

        thread_id = (metadata or {}).get("thread_id") or reply_to
        attachments = [{"type": attachment_type, "link": url}]

        try:
            data = await self._api.send_attachment_v5(
                conversation_id=chat_id,
                attachments=attachments,
                thread_id=thread_id,
            )
            msg_id = first_str(data.get("id"), data.get("message_id"))
            # When the caller passed a caption, follow up with a text
            # message in the same thread so the agent's voice accompanies
            # the attachment instead of leaving the user to guess context.
            if caption and caption.strip():
                await self.send(
                    chat_id=chat_id,
                    content=caption,
                    metadata={"thread_id": thread_id} if thread_id else None,
                )
            return SendResult(success=True, message_id=msg_id, raw_response=data)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            body = exc.response.text if exc.response is not None else ""
            return SendResult(
                success=False,
                error=f"HTTP {status}: {body[:500]}",
                retryable=status in (408, 429, 500, 502, 503, 504),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "carbonvoice", "chat_id": chat_id}

    # ── Thread-context fetch (PR 4) ──────────────────────────────────────
    #
    # When the agent gets @mentioned in a group thread for the first time
    # (no Hermes session yet for that thread), we have no history to feed
    # the LLM — it sees one isolated message and has to guess at context.
    # We fetch the thread's prior messages via CV's REST API and prepend
    # them as a ``[Thread context …]`` block to the user's message so
    # the agent has the prior history from turn 1.
    #
    # The "no active session" guard means this only fires on the first
    # turn in any given thread; every subsequent turn rides on the session
    # history that Hermes core maintains in SQLite, so there is no
    # duplication.
    #
    # CV has no native "list messages in thread" endpoint today, so we
    # combine two calls — a lightweight channel index (just ids +
    # ``parent_message_id``) plus a batched ``by-ids`` fetch — to assemble
    # the thread's transcript. When cv-api adds a direct thread-listing
    # endpoint the workaround collapses to one call; see
    # ``api.list_channel_message_index`` for the full rationale.

    def _has_active_session_for_thread(
        self,
        channel_id: str,
        thread_id: str,
        user_id: str,
    ) -> bool:
        """Return True when a Hermes session already covers this thread.

        Uses ``build_session_key()`` as the single source of truth so the
        key respects ``group_sessions_per_user`` /
        ``thread_sessions_per_user`` exactly the way Hermes core does at
        message-routing time. A drift here would mean we'd inject thread
        context on a turn where Hermes already has session history,
        duplicating the parent in every prompt.
        """
        session_store = getattr(self, "_session_store", None)
        if not session_store:
            return False
        try:
            from gateway.session import build_session_key

            store_cfg = getattr(session_store, "config", None)
            gspu = (
                getattr(store_cfg, "group_sessions_per_user", True)
                if store_cfg
                else True
            )
            tspu = (
                getattr(store_cfg, "thread_sessions_per_user", False)
                if store_cfg
                else False
            )

            source = SessionSource(
                platform=Platform("carbonvoice"),
                chat_id=channel_id,
                chat_type="group",
                user_id=user_id,
                thread_id=thread_id,
            )
            session_key = build_session_key(
                source,
                group_sessions_per_user=gspu,
                thread_sessions_per_user=tspu,
            )

            ensure = getattr(session_store, "_ensure_loaded", None)
            if callable(ensure):
                ensure()
            entries = getattr(session_store, "_entries", None) or {}
            return session_key in entries
        except Exception:
            return False

    async def _fetch_thread_context(
        self,
        channel_id: str,
        thread_id: str,
        current_msg_id: str,
        *,
        limit: int = 30,
    ) -> str:
        """Return a formatted ``[Thread context …]`` prefix for *thread_id*.

        Returns ``""`` (empty string) on any failure or when the thread
        has no prior content — callers should treat empty as "nothing to
        prepend" and pass the original user text through unchanged.

        Steps:
          1. Cache hit via :meth:`ConversationTracker.get_cached_thread_context`.
          2. ``api.list_channel_message_index`` → ids + ``parent_message_id``.
          3. Client-side filter to thread (root + replies whose
             ``parent_message_id == thread_id``).
          4. ``api.get_messages_by_ids_v5`` for the last ``limit``
             transcripts in chronological order.
          5. Exclude the current triggering message (it will be delivered
             as the user message itself) and exclude our own prior bot
             replies (circular context — feeding them back creates an
             echo that the LLM tends to repeat).
          6. Format ``[thread parent] name: text`` for the root and
             ``name: text`` for replies, wrap in the standard delimiters,
             cache, return.
        """
        if self._api is None or not thread_id:
            return ""

        cached = self._tracker.get_cached_thread_context(thread_id)
        if cached is not None:
            return cached

        try:
            index = await self._api.list_channel_message_index(
                channel_id, limit=200, direction="older"
            )
        except Exception as exc:
            logger.debug(
                "carbonvoice: list_channel_message_index(%s) failed: %s",
                channel_id, exc,
            )
            return ""

        if not index:
            return ""

        # Pick out items in this thread: the root and its direct replies.
        # CV is flat (DEVELOPMENT.md §4) so a single equality check on
        # ``parent_message_id`` covers every sibling — no walk needed.
        thread_items = []
        for item in index:
            mid = first_str(
                item.get("message_id"), item.get("_id"), item.get("id"),
            )
            if not mid:
                continue
            parent = first_str(
                item.get("parent_message_id"),
                item.get("parent_message_guid"),
                item.get("thread_id"),
            )
            is_root = mid == thread_id
            is_sibling = parent == thread_id
            if not (is_root or is_sibling):
                continue
            if mid == current_msg_id:
                continue
            thread_items.append((mid, item, is_root))

        if not thread_items:
            # Cache the empty result so we don't refetch on every turn
            # in an otherwise empty thread.
            self._tracker.set_cached_thread_context(thread_id, "")
            return ""

        # Order chronologically. The index endpoint returns ``created_at``
        # as either ISO or epoch ms depending on call; sort lexically when
        # string and numerically when number — both give the right order.
        def _ts(entry):
            ts = entry[1].get("created_at") or entry[1].get("created") or 0
            return ts
        thread_items.sort(key=_ts)

        # Cap to ``limit`` most-recent so a long-running thread doesn't
        # blow the prompt budget. Keep the root if present so context is
        # anchored even when the tail is large.
        if len(thread_items) > limit:
            head = [t for t in thread_items if t[2]][:1]  # the root, if any
            tail = [t for t in thread_items if not t[2]][-(limit - len(head)):]
            thread_items = head + tail

        ids = [mid for mid, _, _ in thread_items]
        try:
            full = await self._api.get_messages_by_ids_v5(ids)
        except Exception as exc:
            logger.debug(
                "carbonvoice: get_messages_by_ids_v5 for thread context failed: %s",
                exc,
            )
            return ""

        # Index by id so we can preserve our chronological order.
        full_by_id = {
            first_str(m.get("id"), m.get("message_id"), m.get("_id")): m
            for m in (full or [])
            if isinstance(m, dict)
        }

        parts = []
        for mid, item, is_root in thread_items:
            msg = full_by_id.get(mid)
            if not msg:
                continue
            text = (extract_transcript(msg) or "").strip()
            if not text:
                continue
            creator = extract_creator_id(msg) or item.get("creator_id") or ""
            # Skip our own prior bot replies — feeding them back as
            # "[bot]: …" creates a circular context the LLM tends to echo.
            # Keep the thread parent even when authored by the bot (e.g.
            # the thread was opened by a cron post we're now replying to).
            if (
                creator
                and self._self_user_id
                and creator == self._self_user_id
                and not is_root
            ):
                continue
            name = creator
            if creator and self._users is not None:
                try:
                    name = await self._users.resolve(creator)
                except Exception:
                    name = creator
            name = name or "unknown"
            # Strip CV's inline @[name](guid) markup for the same reason
            # we strip it on inbound — the guids are LLM noise.
            text = strip_inline_mentions(text)
            prefix = "[thread parent] " if is_root else ""
            parts.append(f"{prefix}{name}: {text}")

        if not parts:
            self._tracker.set_cached_thread_context(thread_id, "")
            return ""

        content = (
            "[Thread context — prior messages in this thread "
            "(not yet in conversation history):]\n"
            + "\n".join(parts)
            + "\n[End of thread context]\n\n"
        )
        self._tracker.set_cached_thread_context(thread_id, content)
        # INFO so it shows up in default gateway.log — operators need to
        # see when context was injected to debug "why did the bot know
        # that?" / "why did the bot miss that?" questions without flipping
        # to DEBUG. Volume is bounded: fires at most once per thread per
        # TTL window (subsequent mentions in the same thread hit the
        # active-session guard and skip this method entirely).
        logger.info(
            "carbonvoice: thread context injected for %s — %d prior message(s), %d chars",
            thread_id, len(parts), len(content),
        )
        return content

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
        # by ``thread_id``, and ``send()`` reads ``metadata['thread_id']``
        # populated by Hermes core from ``SessionSource.thread_id`` — so
        # concurrent threads in the same channel each resolve their own
        # anchor (closes the §7.6 latent bug end-to-end).
        parent = first_str(
            msg.get("parent_message_id"), msg.get("parent_message_guid")
        )
        thread_id = ConversationTracker.thread_id_of(msg)
        if thread_id:
            self._tracker.set_reply_anchor(thread_id, thread_id)

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

        # Thread-context fetch (PR 4): when this is the first @mention in
        # a group thread (no Hermes session yet), pull the prior messages
        # so the agent has context from turn 1. Guard with the
        # "no active session" check so subsequent turns ride on Hermes'
        # SQLite session history without re-injecting the parent each
        # time. DMs skip the fetch — their single session already covers
        # the conversation, and there are no sibling participants whose
        # context we'd be missing.
        if (
            chat_type == "group"
            and session_thread_id
            and creator_id
            and not self._has_active_session_for_thread(
                channel_id, session_thread_id, creator_id,
            )
        ):
            context_prefix = await self._fetch_thread_context(
                channel_id=channel_id,
                thread_id=session_thread_id,
                current_msg_id=message_id,
            )
            if context_prefix:
                clean_text = context_prefix + clean_text

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
