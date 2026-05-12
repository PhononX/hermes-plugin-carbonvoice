"""
Carbon Voice platform adapter for Hermes Agent.

Architecture:
    Hermes  <──Socket.IO (primary)──   api.carbonvoice.app
    Hermes  ── REST poll fallback ──>  /v3/messages/recent
    Hermes  ── POST /v3/messages/start ──>  outbound replies

No public webhook is required — the adapter holds an outbound Socket.IO
connection and polls /v3/messages/recent as a fallback. Cursor state is
persisted to disk so messages received while Hermes was offline are
processed on the next startup.

Spec sourced from the Open Claw `openclaw-carbonvoice` plugin and the
PhononX `cv-claude-channel` reference implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

try:
    import socketio  # python-socketio[asyncio_client]
    SOCKETIO_AVAILABLE = True
except ImportError:
    SOCKETIO_AVAILABLE = False
    socketio = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.carbonvoice.app"
DEFAULT_POLL_INTERVAL_MS = 5_000
DEFAULT_WS_RETRY_INITIAL_MS = 1_000
DEFAULT_WS_RETRY_MAX_MS = 30_000
DEFAULT_SEEN_TTL_S = 5 * 60
DEFAULT_FLUSH_DEBOUNCE_S = 5.0
HTTP_TIMEOUT = 30.0


def _auth_headers(pat: str) -> Dict[str, str]:
    """Carbon Voice accepts PATs via Bearer auth and other keys via x-api-key."""
    trimmed = pat.strip()
    if trimmed.lower().startswith("cv_pat_"):
        return {"Authorization": f"Bearer {trimmed}"}
    return {"x-api-key": trimmed}


def _first_str(*vals: Any) -> Optional[str]:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    except Exception:
        home = Path.home() / ".hermes"
    return home / "state" / "carbonvoice.json"


def _extract_transcript(msg: Dict[str, Any]) -> str:
    """Pull the human-readable transcript from a CV message payload.

    The transcript lives in ``text_models[].timecodes[].t`` joined by spaces;
    the ``value`` field is empty for transcript models. When the message is
    still being transcribed, all transcript fields are empty — the caller
    must treat an empty return as "not ready yet" and retry later.
    """
    text_models = msg.get("text_models") or []
    if isinstance(text_models, list):
        for m in text_models:
            if not isinstance(m, dict):
                continue
            if m.get("type") in ("transcript_with_timecode", "transcript"):
                timecodes = m.get("timecodes") or []
                if isinstance(timecodes, list):
                    joined = " ".join(
                        tc.get("t", "")
                        for tc in timecodes
                        if isinstance(tc, dict) and isinstance(tc.get("t"), str)
                    ).strip()
                    if joined:
                        return joined
                value = m.get("value")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    # Webhook-style payloads use different field names — accept those too.
    fallback = _first_str(msg.get("transcript_txt"), msg.get("ai_summary_txt"))
    return fallback or ""


def check_requirements() -> bool:
    if not HTTPX_AVAILABLE:
        logger.error("carbonvoice: httpx not installed")
        return False
    if not SOCKETIO_AVAILABLE:
        logger.warning(
            "carbonvoice: python-socketio not installed — running in polling-only mode "
            "(install with: pip install 'python-socketio[asyncio_client]')"
        )
    return True


def validate_config(config: PlatformConfig) -> bool:
    """Return True when the config has the minimum required credentials."""
    extra = getattr(config, "extra", {}) or {}
    pat = os.getenv("CARBONVOICE_PAT") or config.token or extra.get("pat", "")
    return bool(pat)


def is_connected(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(config.token or extra.get("pat"))


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed PlatformConfig.extra from env vars before adapter construction."""
    pat = os.getenv("CARBONVOICE_PAT")
    if not pat:
        return None

    seed: Dict[str, Any] = {
        "pat": pat,
        "base_url": (os.getenv("CARBONVOICE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        "poll_interval_ms": int(
            os.getenv("CARBONVOICE_POLL_INTERVAL_MS") or DEFAULT_POLL_INTERVAL_MS
        ),
        "ws_retry_max_ms": int(
            os.getenv("CARBONVOICE_WS_RETRY_MAX_MS") or DEFAULT_WS_RETRY_MAX_MS
        ),
        "creator_id": os.getenv("CARBONVOICE_CREATOR_ID") or None,
        "state_path": os.getenv("CARBONVOICE_STATE_PATH") or None,
    }

    home_channel_id = os.getenv("CARBONVOICE_HOME_CHANNEL")
    if home_channel_id:
        seed["home_channel"] = {
            "chat_id": home_channel_id,
            "name": os.getenv("CARBONVOICE_HOME_CHANNEL_NAME") or home_channel_id,
        }
    return seed


class CarbonVoiceAdapter(BasePlatformAdapter):
    """Hermes ↔ Carbon Voice bridge over Socket.IO + REST polling fallback."""

    MAX_MESSAGE_LENGTH = 8000

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("carbonvoice"))
        extra = config.extra or {}
        self._pat: str = config.token or extra.get("pat") or ""
        self._base_url: str = (extra.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._poll_interval_s: float = (
            float(extra.get("poll_interval_ms") or DEFAULT_POLL_INTERVAL_MS) / 1000.0
        )
        self._ws_retry_max_s: float = (
            float(extra.get("ws_retry_max_ms") or DEFAULT_WS_RETRY_MAX_MS) / 1000.0
        )
        self._creator_id: Optional[str] = extra.get("creator_id") or None
        sp = extra.get("state_path")
        self._state_path: Path = (
            Path(sp).expanduser() if sp else _default_state_path()
        )

        self._self_user_id: Optional[str] = None
        self._client: Optional["httpx.AsyncClient"] = None
        self._sio: Optional["socketio.AsyncClient"] = None

        # Background tasks
        self._poll_task: Optional[asyncio.Task] = None
        self._ws_reconnect_task: Optional[asyncio.Task] = None
        self._flush_task: Optional[asyncio.Task] = None

        # Connection mode: connecting | websocket | polling | shutdown
        self._mode: str = "connecting"
        self._ws_retry_backoff_s: float = DEFAULT_WS_RETRY_INITIAL_MS / 1000.0

        # Cursor + dedupe
        self._last_seen_at: Optional[str] = None
        self._state_dirty: bool = False
        self._seen: Dict[str, float] = {}  # message_id → expiry epoch seconds

        # Threading helper for reply_to_message_id on outbound sends.
        self._last_inbound_msg: Dict[str, str] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        if not self._pat:
            logger.error("carbonvoice: CARBONVOICE_PAT not set")
            return False

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=_auth_headers(self._pat),
            timeout=HTTP_TIMEOUT,
        )

        try:
            self._self_user_id = await self._whoami()
        except Exception as exc:
            logger.error("carbonvoice: /whoami failed: %s", exc)
            await self._close_client()
            return False
        if not self._self_user_id:
            logger.error("carbonvoice: /whoami returned no user id")
            await self._close_client()
            return False

        await self._load_state()

        try:
            await self._fetch_missed_messages()
        except Exception as exc:
            logger.warning("carbonvoice: initial catch-up failed: %s", exc)

        if SOCKETIO_AVAILABLE:
            try:
                await self._connect_websocket()
            except Exception as exc:
                logger.warning(
                    "carbonvoice: WS initial connect failed (%s) — using polling", exc
                )
                self._mode = "polling"
                self._start_polling()
                self._schedule_ws_reconnect()
        else:
            self._mode = "polling"
            self._start_polling()

        self._mark_connected()
        logger.info(
            "carbonvoice: connected as %s (mode=%s, state=%s)",
            self._self_user_id, self._mode, self._state_path,
        )
        return True

    async def disconnect(self) -> None:
        self._mode = "shutdown"

        tasks = [
            t for t in (self._poll_task, self._ws_reconnect_task, self._flush_task)
            if t is not None and not t.done()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._poll_task = None
        self._ws_reconnect_task = None
        self._flush_task = None

        if self._sio is not None:
            try:
                await self._sio.disconnect()
            except Exception:
                pass
            self._sio = None

        # Final flush of any pending cursor advances.
        self._state_dirty = True
        await self._flush_state()

        await self._close_client()
        self._mark_disconnected()

    async def _close_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ── Outbound (Hermes → Carbon Voice) ─────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="adapter not connected")
        if not content or not content.strip():
            return SendResult(success=False, error="empty content")

        reply_target = reply_to or self._last_inbound_msg.get(chat_id)

        body = {
            "unique_client_id": str(uuid.uuid4()),
            "transcript": content,
            "is_text_message": True,
            "is_streaming": False,
            "channel_id": chat_id.strip(),
        }
        if reply_target:
            body["reply_to_message_id"] = str(reply_target)

        try:
            resp = await self._client.post("/v3/messages/start", json=body)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            msg_id = _first_str(data.get("message_id"), data.get("id"))
            return SendResult(success=True, message_id=msg_id, raw_response=data)
        except httpx.HTTPStatusError as exc:
            text = exc.response.text[:500] if exc.response is not None else ""
            return SendResult(
                success=False,
                error=f"HTTP {exc.response.status_code}: {text}",
                retryable=exc.response.status_code in (408, 429, 500, 502, 503, 504),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "carbonvoice", "chat_id": chat_id}

    # ── REST helpers ─────────────────────────────────────────────────────

    async def _whoami(self) -> Optional[str]:
        assert self._client is not None
        resp = await self._client.get("/whoami")
        resp.raise_for_status()
        data = resp.json() or {}
        user = data.get("user") or {}
        return _first_str(user.get("user_guid"), user.get("_id"), user.get("id"))

    # ── State persistence ────────────────────────────────────────────────

    async def _load_state(self) -> None:
        try:
            raw = self._state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            last = data.get("lastSeenAt")
            if isinstance(last, str) and last:
                self._last_seen_at = last
                logger.info("carbonvoice: resuming from %s", last)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("carbonvoice: failed to load state: %s", exc)

    async def _flush_state(self) -> None:
        if not self._state_dirty or self._last_seen_at is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"lastSeenAt": self._last_seen_at}),
                encoding="utf-8",
            )
            self._state_dirty = False
        except Exception as exc:
            logger.warning("carbonvoice: failed to flush state: %s", exc)

    def _schedule_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            return

        async def _delayed():
            try:
                await asyncio.sleep(DEFAULT_FLUSH_DEBOUNCE_S)
                await self._flush_state()
            except asyncio.CancelledError:
                pass

        self._flush_task = asyncio.create_task(_delayed())

    def _advance_cursor(self, iso_ts: str) -> None:
        self._last_seen_at = iso_ts
        self._state_dirty = True
        self._schedule_flush()

    # ── Dedupe (in-memory, TTL-based) ────────────────────────────────────

    def _is_recently_seen(self, message_id: str) -> bool:
        exp = self._seen.get(message_id)
        if exp is None:
            return False
        if time.time() > exp:
            self._seen.pop(message_id, None)
            return False
        return True

    def _mark_seen(self, message_id: str) -> None:
        self._seen[message_id] = time.time() + DEFAULT_SEEN_TTL_S
        if len(self._seen) % 100 == 0:
            now = time.time()
            for mid, exp in list(self._seen.items()):
                if now > exp:
                    self._seen.pop(mid, None)

    # ── Core: process a single inbound message ───────────────────────────

    async def _process_message(self, msg: Dict[str, Any]) -> bool:
        message_id = _first_str(msg.get("message_id"), msg.get("_id"))
        if not message_id:
            return False

        channel_ids = msg.get("channel_ids")
        if isinstance(channel_ids, list) and channel_ids:
            channel_id = channel_ids[0]
        else:
            channel_id = _first_str(msg.get("channel_id"), msg.get("channel_guid"))
        if not channel_id:
            return False

        creator_id = _first_str(msg.get("creator_id"), msg.get("creator_guid"))

        # Self-loop guard.
        if creator_id and self._self_user_id and creator_id == self._self_user_id:
            return False

        # Optional single-user restriction (acts before transcript check so
        # we don't waste cycles on transcripts we'll drop anyway).
        if self._creator_id and creator_id and creator_id != self._creator_id:
            return False

        # Dedupe early so retries on still-transcribing messages don't multiply.
        if self._is_recently_seen(message_id):
            return False

        # Two-phase transcript: empty means "not ready yet" — don't mark seen.
        transcript = _extract_transcript(msg)
        if not transcript:
            return False

        self._mark_seen(message_id)

        parent_guid = _first_str(
            msg.get("parent_message_id"), msg.get("parent_message_guid")
        )
        reply_anchor = parent_guid or message_id
        self._last_inbound_msg[channel_id] = message_id

        source = SessionSource(
            platform=Platform("carbonvoice"),
            chat_id=channel_id,
            chat_name=f"cv:{channel_id[:8]}",
            chat_type="dm",
            user_id=creator_id or "",
            user_name=creator_id or "",
            message_id=message_id,
        )
        event = MessageEvent(
            text=transcript,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=msg,
            message_id=message_id,
            reply_to_message_id=(
                reply_anchor if reply_anchor != message_id else None
            ),
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

    # ── Catch-up via REST ────────────────────────────────────────────────

    async def _fetch_missed_messages(self) -> None:
        if self._client is None:
            return

        request_started_at = _now_iso()

        if not self._last_seen_at:
            logger.info("carbonvoice: first run, starting from %s", request_started_at)
            self._advance_cursor(request_started_at)
            return

        body = {
            "date": self._last_seen_at,
            "direction": "newer",
            "limit": 100,
            "use_last_updated": False,
        }
        try:
            resp = await self._client.post("/v3/messages/recent", json=body)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("carbonvoice: /v3/messages/recent failed: %s", exc)
            return  # don't advance cursor — retry same window next tick

        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("carbonvoice: /v3/messages/recent bad JSON: %s", exc)
            return

        messages = data if isinstance(data, list) else []
        messages.sort(key=lambda m: m.get("created_at") or "")

        for msg in messages:
            try:
                await self._process_message(msg)
            except Exception as exc:
                logger.error("carbonvoice: process_message error: %s", exc)

        # Advance cursor to when this request was fired, not message timestamps —
        # avoids missing concurrent writes that landed during the call.
        self._advance_cursor(request_started_at)

    # ── Polling loop ─────────────────────────────────────────────────────

    def _start_polling(self) -> None:
        if self._poll_task and not self._poll_task.done():
            return
        logger.info("carbonvoice: polling every %.1fs", self._poll_interval_s)

        async def _tick():
            try:
                while self._mode not in ("shutdown", "websocket"):
                    try:
                        await self._fetch_missed_messages()
                    except Exception as exc:
                        logger.warning("carbonvoice: poll tick failed: %s", exc)
                    await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                pass

        self._poll_task = asyncio.create_task(_tick())

    def _stop_polling(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            logger.info("carbonvoice: polling stopped (WS active)")
        self._poll_task = None

    # ── Socket.IO (primary transport) ────────────────────────────────────

    async def _connect_websocket(self) -> None:
        if not SOCKETIO_AVAILABLE:
            raise RuntimeError("python-socketio not installed")

        sio = socketio.AsyncClient(reconnection=False)
        self._sio = sio

        @sio.on("connect")
        async def _on_connect():  # noqa: F811
            logger.info("carbonvoice: Socket.IO connected")
            self._mode = "websocket"
            self._ws_retry_backoff_s = DEFAULT_WS_RETRY_INITIAL_MS / 1000.0
            self._stop_polling()

        @sio.on("connected")
        async def _on_connected(user: Any = None):  # noqa: F811
            if isinstance(user, dict):
                uid = _first_str(user.get("user_guid"), user.get("id"))
                if uid and not self._self_user_id:
                    self._self_user_id = uid

        async def _on_message_event(payload: Any = None):
            if not isinstance(payload, dict):
                return
            # message:created fires before the transcript is ready;
            # message:updated fires once transcription completes.
            if payload.get("status") != "active":
                return
            try:
                await self._fetch_missed_messages()
            except Exception as exc:
                logger.warning("carbonvoice: catch-up after WS event failed: %s", exc)

        sio.on("message:created", _on_message_event)
        sio.on("message:updated", _on_message_event)

        @sio.on("disconnect")
        async def _on_disconnect():  # noqa: F811
            if self._mode == "shutdown":
                return
            logger.warning(
                "carbonvoice: Socket.IO disconnected — falling back to polling"
            )
            self._mode = "polling"
            try:
                await self._fetch_missed_messages()
            except Exception:
                pass
            self._start_polling()
            self._schedule_ws_reconnect()

        await sio.connect(
            self._base_url,
            auth={"authorization": f"Bearer {self._pat}"},
            transports=["websocket"],
        )

    def _schedule_ws_reconnect(self) -> None:
        if self._ws_reconnect_task and not self._ws_reconnect_task.done():
            return
        if self._mode == "shutdown" or not SOCKETIO_AVAILABLE:
            return

        async def _reconnect():
            try:
                while self._mode not in ("shutdown", "websocket"):
                    await asyncio.sleep(self._ws_retry_backoff_s)
                    if self._mode == "shutdown":
                        return
                    logger.info(
                        "carbonvoice: attempting WS reconnect (backoff %.1fs)",
                        self._ws_retry_backoff_s,
                    )
                    try:
                        await self._fetch_missed_messages()
                        await self._connect_websocket()
                        return  # connected — exit loop
                    except Exception as exc:
                        logger.debug("carbonvoice: WS reconnect failed: %s", exc)
                        self._ws_retry_backoff_s = min(
                            self._ws_retry_backoff_s * 2,
                            self._ws_retry_max_s,
                        )
            except asyncio.CancelledError:
                pass

        self._ws_reconnect_task = asyncio.create_task(_reconnect())


# ── Standalone sender for out-of-process delivery (cron) ───────────────────

async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    content: str,
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Send a single Carbon Voice message without instantiating the full adapter."""
    if not HTTPX_AVAILABLE:
        return {"success": False, "error": "httpx not installed"}
    extra = pconfig.extra or {}
    pat = pconfig.token or extra.get("pat")
    base_url = (extra.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    if not pat:
        return {"success": False, "error": "missing CARBONVOICE_PAT"}

    body = {
        "unique_client_id": str(uuid.uuid4()),
        "transcript": content,
        "is_text_message": True,
        "is_streaming": False,
        "channel_id": chat_id.strip(),
    }
    async with httpx.AsyncClient(
        base_url=base_url, headers=_auth_headers(pat), timeout=HTTP_TIMEOUT
    ) as client:
        try:
            resp = await client.post("/v3/messages/start", json=body)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            return {
                "success": True,
                "message_id": _first_str(data.get("message_id"), data.get("id")),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


def interactive_setup() -> Optional[Dict[str, str]]:
    """Lightweight wizard: gather PAT."""
    try:
        pat = input("Carbon Voice PAT (cv_pat_...): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not pat:
        return None
    return {"CARBONVOICE_PAT": pat}


def register(ctx) -> None:
    """Called by the Hermes plugin system on discovery."""
    ctx.register_platform(
        name="carbonvoice",
        label="Carbon Voice",
        adapter_factory=lambda cfg: CarbonVoiceAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["CARBONVOICE_PAT"],
        install_hint=(
            "pip install httpx 'python-socketio[asyncio_client]' "
            "(python-socketio is optional — polling-only without it)"
        ),
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="CARBONVOICE_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="CARBONVOICE_ALLOWED_USERS",
        allow_all_env="CARBONVOICE_ALLOW_ALL_USERS",
        max_message_length=8000,
        emoji="🎙️",
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Carbon Voice. Users send voice or text; "
            "Carbon Voice transcribes voice to text before delivering it to you, "
            "and your text replies are rendered as messages (Carbon Voice handles "
            "any TTS playback on the user side). Plain text and lightweight "
            "markdown render best — avoid complex tables, multi-column layouts, "
            "or raw HTML. Keep responses conversational and concise."
        ),
    )
