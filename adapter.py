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
import mimetypes
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
    extract_attachments,
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

    # Voice-out integration with Hermes core's auto-TTS pipeline.
    #
    # When core generates a TTS audio for the agent's reply and ships it
    # via ``send_voice`` → ``/v5/messages/audio``, Carbon Voice runs
    # server-side STT and renders the resulting message as a voice-memo
    # bubble with the transcript inline. That means the spoken text IS
    # the visible text — sending the same content again as a text bubble
    # is pure duplication.
    #
    # ``voice_out_carries_text = True`` tells Hermes core (see
    # ``gateway/platforms/base.py``'s ``_tts_caption_delivered`` check)
    # to suppress the follow-up text send when auto-TTS succeeded.
    # Conceptually it's the CV analog of Telegram's caption field on
    # voice messages — different mechanism (STT vs caption), same UX
    # contract (one bubble, text + audio together).
    #
    # The base class default is False, so adapters that don't override
    # this are unaffected. Requires the patched base.py from PR 6 (and
    # the parallel upstream PR) — without it the attribute is read but
    # ignored, and we ship a duplicate text bubble.
    voice_out_carries_text = True

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
        # Voice-out: when true, every inbound MessageEvent is marked
        # ``MessageType.VOICE`` so Hermes core's auto-TTS pipeline
        # (``base.py:3493``) converts the agent's text reply to audio
        # and ships it via :meth:`send_voice` → ``/v5/messages/audio``.
        # Requires ``voice.auto_tts: true`` and a TTS provider in
        # ``config.yaml`` to actually fire — without those, marking
        # VOICE is a no-op (the gate's other conditions still fail).
        # Default ``False`` to preserve text-out for existing
        # deployments that haven't opted in.
        self._voice_out: bool = bool(extra.get("voice_out"))
        # Inbound multimodal (PR 7): per-attachment byte cap. CV's S3
        # URLs can hand back arbitrarily large files, and Hermes core's
        # vision / document pipeline pays per token for image bytes and
        # extracted text — a 50MB PDF blowing through the size limit
        # crashes the agent's API call. Default 10 MB matches what
        # Claude / OpenAI vision recommend; operators can raise it for
        # specialized use cases via ``CARBONVOICE_MAX_ATTACHMENT_MB``.
        self._max_attachment_bytes: int = int(
            extra.get("max_attachment_mb") or 10
        ) * 1024 * 1024

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
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send a voice memo via ``POST /v5/messages/audio`` (multipart).

        ``audio_path`` is a local audio file. CV transcribes it
        server-side and threads the resulting message using ``thread_id``
        from metadata (same resolution rules as :meth:`send`).

        Parameter names match :class:`BasePlatformAdapter.send_voice` —
        Hermes core's media dispatch (``base.py:3640``) invokes us with
        the keyword ``audio_path=``, so renaming this would break the
        agent's "MEDIA:/foo.mp3 in reply" flow. ``caption`` is accepted
        for signature compatibility but currently ignored (CV's audio
        endpoint doesn't take a caption — the transcript IS the caption).
        """
        if self._api is None:
            return SendResult(success=False, error="adapter not connected")
        thread_id = (metadata or {}).get("thread_id") or reply_to
        try:
            data = await self._api.send_audio_v5(
                conversation_id=chat_id,
                audio_path=audio_path,
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
        """Attach an image to the conversation.

        ``image_url`` accepts either a publicly-resolvable URL or a path
        to a local file. URL → single attachment with ``type:"link"``.
        Local file → 4-step signed-URL flow (see
        :meth:`_send_file_or_link`). ``caption`` becomes the transcript
        on the same bubble — agent text and image arrive together.
        """
        return await self._send_file_or_link(
            chat_id=chat_id,
            path_or_url=image_url,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Attach a local image file (``.jpg``, ``.png``, ``.webp``, ...).

        Hermes core's media dispatch wraps local image paths as
        ``file://...`` URIs and routes them through
        :meth:`BasePlatformAdapter.send_multiple_images`, whose default
        implementation calls :meth:`send_image_file` per item. Without
        this override the agent's "MEDIA:/foo.png" flow would fall back
        to "🖼️ Image: /foo.png" plain-text from the base class — useless
        on CV. Routes through the same signed-URL flow as
        :meth:`send_document`; the file just happens to be an image.
        """
        return await self._send_file_or_link(
            chat_id=chat_id,
            path_or_url=image_path,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Attach a document (any non-image file) to the conversation.

        Same mechanics as :meth:`send_image` — both go through the
        ``type:"file"`` attachment shape on CV; the difference is purely
        in the Hermes core method dispatch. Use this for ``.md``, PDFs,
        archives, audio clips not meant as voice memos, etc. For voice
        memos (transcribed server-side) use :meth:`send_voice`.

        Parameter names match :class:`BasePlatformAdapter.send_document`
        so Hermes core's media dispatch (``base.py:3652``) reaches us
        with the right keywords. ``file_name``, if provided, overrides
        the on-disk basename when building the attachment payload — e.g.
        for renaming ``/tmp/tmpXYZ`` to ``report.md`` on the recipient
        side.
        """
        return await self._send_file_or_link(
            chat_id=chat_id,
            path_or_url=file_path,
            caption=caption,
            file_name=file_name,
            reply_to=reply_to,
            metadata=metadata,
        )

    # ── Attachment flow (URL or local file) ─────────────────────────────
    #
    # Mirrors the Flutter client's pattern: the agent sees its message
    # appear in the conversation immediately with an "Initializing"
    # placeholder, while the actual S3 upload runs in the background and
    # flips the status to ``Uploaded`` (or ``Failed``) when it settles.
    #
    # URL inputs skip the upload entirely — they just attach the URL
    # with ``type:"link"`` since the file is already hosted somewhere
    # the recipient can fetch.

    @staticmethod
    def _is_url(path_or_url: str) -> bool:
        return path_or_url.startswith(("http://", "https://"))

    @staticmethod
    def _guess_mime(path: Path) -> str:
        """Best-effort MIME type from filename extension.

        ``mimetypes`` ships a tiny built-in DB plus the system's
        ``/etc/mime.types``. We add ``.md`` → ``text/markdown`` because
        the stdlib still classifies markdown as ``text/x-markdown`` on
        some platforms and ``None`` on others; ``text/markdown`` is the
        IANA-registered form (RFC 7763) and what the agent's tooling
        will actually produce.
        """
        if path.suffix.lower() == ".md":
            return "text/markdown"
        guessed, _ = mimetypes.guess_type(str(path))
        return guessed or "application/octet-stream"

    async def _send_file_or_link(
        self,
        *,
        chat_id: str,
        path_or_url: str,
        caption: Optional[str],
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
        file_name: Optional[str] = None,
    ) -> SendResult:
        if self._api is None:
            return SendResult(success=False, error="adapter not connected")
        if not path_or_url:
            return SendResult(success=False, error="attachment path/URL required")

        thread_id = (metadata or {}).get("thread_id") or reply_to
        caption_text = (caption or "").strip()

        try:
            if self._is_url(path_or_url):
                attachment = {"type": "link", "link": path_or_url}
                data = await self._create_attachment_message(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    caption=caption_text,
                    attachment=attachment,
                )
                msg_id = first_str(data.get("id"), data.get("message_id"))
                return SendResult(success=True, message_id=msg_id, raw_response=data)

            # Local file: signed URL → message-create with Initializing →
            # background S3 PUT + status update.
            path = Path(path_or_url).expanduser()
            if not path.is_file():
                return SendResult(
                    success=False, error=f"file not found: {path}",
                )
            mime_type = self._guess_mime(path)
            # Caller may override the basename so a temp path like
            # ``/tmp/tmpXYZ`` shows up as ``report.md`` on the recipient.
            filename = file_name or path.name

            urls = await self._api.get_signed_upload_urls(
                [{"filename": filename, "mimetype": mime_type}],
            )
            if not urls or not urls[0].get("url"):
                return SendResult(
                    success=False,
                    error="signedurl: empty response from /v3/attachments/signedurl",
                )
            signed_url = urls[0]["url"]
            canonical_link = signed_url.split("?", 1)[0]

            attachment = {
                "type": "file",
                "link": canonical_link,
                "filename": filename,
                "mime_type": mime_type,
                "status": "Initializing",
                "percent_complete": 0,
            }
            try:
                attachment["length_in_bytes"] = path.stat().st_size
            except OSError:
                pass  # non-fatal; server tolerates missing size

            data = await self._create_attachment_message(
                chat_id=chat_id,
                thread_id=thread_id,
                caption=caption_text,
                attachment=attachment,
            )
            msg_id = first_str(data.get("id"), data.get("message_id"))

            # Find the just-created attachment id in the response so the
            # background task can flip its status when S3 settles. The
            # server returns ``attachments[]`` in the order we sent them,
            # so the first/only entry is ours.
            created_attachments = data.get("attachments") or []
            attachment_id: Optional[str] = None
            if created_attachments:
                first_att = created_attachments[0]
                if isinstance(first_att, dict):
                    attachment_id = first_str(
                        first_att.get("id"), first_att.get("_id"),
                    )

            if attachment_id:
                base_body = {
                    "type": "file",
                    "link": canonical_link,
                    "filename": filename,
                    "mime_type": mime_type,
                }
                # Fire-and-forget — survives this method returning.
                asyncio.create_task(
                    self._upload_attachment_in_background(
                        signed_url=signed_url,
                        file_path=str(path),
                        mime_type=mime_type,
                        message_id=msg_id or "",
                        attachment_id=attachment_id,
                        base_body=base_body,
                    )
                )
            else:
                logger.warning(
                    "carbonvoice: no attachment id in response for %s — "
                    "skipping background upload + status update (message "
                    "will show 'Initializing' indefinitely on the recipient)",
                    filename,
                )

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

    async def _create_attachment_message(
        self,
        *,
        chat_id: str,
        thread_id: Optional[str],
        caption: str,
        attachment: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create the message that carries *attachment*.

        Routes based on whether the caller supplied a caption:
          - caption present → ``POST /v5/messages/text`` with
            ``transcript`` + ``attachments`` (the server requires
            ``transcript`` to be non-empty on this endpoint).
          - caption absent → ``POST /v5/messages/attachment``
            (attachment-only message, no transcript).
        """
        if caption:
            return await self._api.send_text_v5(
                conversation_id=chat_id,
                transcript=caption,
                thread_id=thread_id,
                attachments=[attachment],
            )
        return await self._api.send_attachment_v5(
            conversation_id=chat_id,
            attachments=[attachment],
            thread_id=thread_id,
        )

    async def _upload_attachment_in_background(
        self,
        *,
        signed_url: str,
        file_path: str,
        mime_type: str,
        message_id: str,
        attachment_id: str,
        base_body: Dict[str, Any],
    ) -> None:
        """Push the bytes to S3 then flip the attachment status.

        Runs detached from ``send_document``/``send_image`` so the agent
        gets ``SendResult(success=True)`` immediately — the recipient
        sees the message bubble appear with an ``Initializing``
        placeholder and the file fills in once S3 acks. Mirrors how the
        Flutter client behaves on send.

        On S3 failure we PUT ``status:"Failed"`` so the recipient's UI
        renders a clear error state rather than a perpetual spinner.
        Both branches are wrapped in try/except — a transient failure on
        the status-update PUT must not crash the gateway event loop.
        """
        try:
            await self._api.upload_to_s3(signed_url, file_path, mime_type)
        except Exception as exc:
            logger.warning(
                "carbonvoice: S3 upload failed for %s (msg=%s att=%s): %s",
                file_path, message_id, attachment_id, exc,
            )
            try:
                await self._api.update_attachment(
                    message_id,
                    attachment_id,
                    {**base_body, "status": "Failed", "percent_complete": 0},
                )
            except Exception as inner:
                logger.warning(
                    "carbonvoice: update_attachment(Failed) also failed for "
                    "%s: %s", attachment_id, inner,
                )
            return

        try:
            await self._api.update_attachment(
                message_id,
                attachment_id,
                {**base_body, "status": "Uploaded", "percent_complete": 100},
            )
            logger.info(
                "carbonvoice: attachment uploaded — msg=%s att=%s file=%s",
                message_id, attachment_id, file_path,
            )
        except Exception as exc:
            logger.warning(
                "carbonvoice: update_attachment(Uploaded) failed for %s: %s — "
                "S3 upload itself succeeded; recipient may see stale "
                "'Initializing' status",
                attachment_id, exc,
            )

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

    # ── Inbound multimodal (PR 7) ────────────────────────────────────────
    #
    # CV inbound payloads carry ``attachments[]`` whose ``link`` is the
    # canonical S3 URL — auth-gated, returns 403 to unauthenticated
    # requests. To consume them we resolve a signed S3 GET URL via
    # ``GET /attachments/signedurl/:_id`` (authenticated with our PAT),
    # download the bytes to ``IMAGE_CACHE_DIR``, and return ``file://``
    # URIs for Hermes core to inject into the agent's multimodal
    # context (Claude vision sees the bytes inline).
    #
    # Scope for v1: ``image/*`` only. Other mime types (PDFs,
    # ``text/*``, binaries) are dropped with a WARNING because Hermes
    # core has no native document-extraction pipeline today. Without
    # one, the agent receives a ``file://...pdf`` path it can't
    # natively read — it reaches for ``read_file`` (returns binary
    # garbage), then ``terminal`` (asks the operator to approve
    # ``pdftotext`` / similar), then ``execute_code`` (tries Python
    # parsers that may not be installed). Net UX: the user gets a
    # permission prompt instead of an answer. Better to skip cleanly
    # and document the gap.
    #
    # Document support is queued for a follow-up PR that adds an
    # extraction pass (likely via ``pypdf`` + ``markdown`` / ``html``
    # parsers) and prepends the extracted text into the agent's
    # message context the same way thread context is prepended today.
    # Audio attachments live in ``audio_models[]``, not
    # ``attachments[]``; the transcript is already extracted via
    # :func:`extract_transcript`.

    async def _collect_inbound_media(
        self, msg: Dict[str, Any]
    ) -> "tuple[list[str], list[str], list[str]]":
        """Process inbound attachments and return three lists:

          - ``media_urls``  — ``file://`` URIs of downloaded image
            files, ready for ``MessageEvent.media_urls``
          - ``media_types`` — parallel list of mime types
          - ``link_urls``   — bare URLs from ``type:"link"``
            attachments (CV's link-sharing UI flow), to be prepended
            to the agent's message text so it sees them the same way
            it would see a URL the user typed inline

        ``type:"link"`` entries are not downloaded — they don't
        reference uploaded files, they're URLs to external resources.
        Threading them into the text channel lets the agent reach for
        its own browser / fetch tools the same way it does for URLs
        embedded in the transcript directly.
        """
        if self._api is None:
            return [], [], []

        attachments = extract_attachments(msg)
        if not attachments:
            return [], [], []

        # Import the cache dir constant from core so downloaded files
        # land in a root the media-delivery validator already allows.
        # Local import keeps this module gateway-free at import time
        # (CI imports the plugin without core).
        from gateway.platforms.base import IMAGE_CACHE_DIR

        media_urls: list[str] = []
        media_types: list[str] = []
        link_urls: list[str] = []

        for att in attachments:
            aid = att.get("_id") or ""
            mime = (att.get("mime_type") or "").lower()
            att_type = (att.get("type") or "").lower()
            link = att.get("link") or ""
            filename = att.get("filename") or aid or "attachment.bin"

            # CV's link attachment: the user picked "share a URL" in
            # the UI. ``link`` is the actual external URL (not an S3
            # path); ``mime_type`` is null. Surface the URL inline so
            # the agent can reach for its existing web tools just like
            # it would for a URL typed in the transcript directly.
            if att_type == "link":
                if link:
                    link_urls.append(link)
                    logger.info(
                        "carbonvoice: inbound link attachment surfaced "
                        "to agent — %s", link,
                    )
                else:
                    logger.warning(
                        "carbonvoice: skipping link attachment %s — "
                        "no link URL in payload", filename,
                    )
                continue

            if mime.startswith("image/"):
                target_dir = IMAGE_CACHE_DIR
            else:
                logger.warning(
                    "carbonvoice: skipping inbound attachment %s (%s) — "
                    "only image/* is wired in this plugin version "
                    "(document pipeline pending — see DEVELOPMENT.md §4)",
                    filename, mime or "no-mime",
                )
                continue

            if not aid:
                logger.warning(
                    "carbonvoice: skipping inbound attachment %s — "
                    "no attachment_id to resolve a signed URL",
                    filename,
                )
                continue

            try:
                local_path = await self._api.download_attachment(
                    aid,
                    target_dir,
                    filename=filename,
                    max_bytes=self._max_attachment_bytes,
                )
            except ValueError as exc:
                # Size cap hit.
                logger.warning(
                    "carbonvoice: skipping oversized inbound attachment %s: %s",
                    filename, exc,
                )
                continue
            except Exception as exc:
                logger.warning(
                    "carbonvoice: failed to download inbound attachment "
                    "%s (%s): %s", filename, aid, exc,
                )
                continue

            # ``file://`` URI is what Hermes core's media routing expects
            # for locally-cached paths (see ``validate_media_delivery_path``
            # in ``gateway/platforms/base.py``).
            media_urls.append(f"file://{local_path}")
            media_types.append(mime)
            logger.info(
                "carbonvoice: inbound attachment downloaded — "
                "att=%s mime=%s path=%s",
                aid, mime, local_path,
            )

        return media_urls, media_types, link_urls

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

        # V5 source-of-truth enrichment. The socket / v3-poll push gives
        # us a V2-shaped payload that trails the v5 GET on async fields:
        # ``tagged_user_ids`` is empty here until a backend job resolves
        # the tag picker selection, and attachment metadata can lag the
        # same way. CV's v5 endpoint is the canonical post-resolution
        # state — the Flutter client follows the same "socket = signal,
        # REST = truth" pattern.
        #
        # We do the GET only here, after the cheap-reject gates above
        # (self-loop, allowlist, dedupe, empty-transcript), so empty
        # ``message:created`` events don't pay the HTTP. On fetch
        # failure we keep the V2 payload — defensive, so a transient
        # /v5 hiccup doesn't drop an otherwise-deliverable message.
        # The parse helpers (``extract_*``) prefer V5 fields when
        # present, so reassigning ``msg`` is enough — no further
        # downstream changes needed.
        if self._api is not None:
            try:
                enriched = await self._api.get_message_v5(message_id)
            except Exception as exc:
                logger.debug(
                    "carbonvoice: v5 enrichment failed for %s: %s — "
                    "continuing with v2 payload",
                    message_id, exc,
                )
                enriched = None
            if enriched:
                msg = enriched
                # Re-pull transcript from the (canonical) v5 payload —
                # usually the same string but keeps everything in one
                # shape after this point.
                transcript = extract_transcript(msg) or transcript

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
        # Inbound multimodal (PR 7): pull any attached files into local
        # caches so Hermes core's vision pipeline can consume them. CV's
        # S3 URLs need auth, so we resolve a signed GET URL per file
        # attachment, download via that, and hand Hermes core a
        # ``file://`` URI in ``media_urls``. Image attachments are
        # routed to vision; ``type:"link"`` attachments (CV's link-
        # sharing UI) return their URLs in ``link_urls`` so we can
        # prepend them to the visible text — the agent then sees them
        # the same way it sees URLs typed inline, and uses its existing
        # browser / fetch tools to consume them. Anything else (PDFs,
        # binaries, …) is dropped with a WARNING.
        media_urls, media_types, link_urls = await self._collect_inbound_media(msg)

        # If CV's link-share UI was used, surface the URL(s) inline so
        # the agent can fetch them naturally. Prepending preserves the
        # user's own text right after, so the agent reads:
        #
        #     [Attached link: https://...]
        #     <user's actual message>
        if link_urls:
            link_prefix = "\n".join(
                f"[Attached link: {u}]" for u in link_urls
            )
            clean_text = f"{link_prefix}\n{clean_text}" if clean_text else link_prefix

        # Mark VOICE when ``CARBONVOICE_VOICE_OUT=true`` so Hermes core's
        # auto-TTS gate (``base.py:3493``) accepts this event for voice-
        # mode dispatch. CV doesn't distinguish text-typed vs voice-
        # transcribed at the outbound layer (everything ends up as
        # either a text bubble or a voice memo bubble), so applying
        # VOICE to every inbound is the right abstraction for a
        # voice-first platform — the operator opts in once and gets a
        # consistent symmetric experience.
        msg_type = MessageType.VOICE if self._voice_out else MessageType.TEXT
        event = MessageEvent(
            text=clean_text,
            message_type=msg_type,
            source=source,
            raw_message=msg,
            message_id=message_id,
            reply_to_message_id=parent,
            reply_to_text=reply_to_text,
            media_urls=media_urls,
            media_types=media_types,
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
