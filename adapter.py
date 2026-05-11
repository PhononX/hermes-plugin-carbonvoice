"""
Carbon Voice platform adapter for Hermes Agent.

Architecture:
    Carbon Voice  ──POST /apps/subscribe──>  registers our webhook URL
    Carbon Voice  ──POST <webhook_url>──>    inbound user messages
    Hermes agent  ──POST /v3/messages/start──>  outbound replies

Receives `message.posted.to.channel` webhook events (Carbon Voice
transcribes voice to text before delivery, so this adapter is text-only).
Replies via POST /v3/messages/start with `is_text_message: true`.

Spec sourced from the Open Claw `openclaw-carbonvoice` plugin
(2026.3.14).  See ~/.claude memory `project_carbonvoice_api.md` for the
full contract.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import uuid
from typing import Any, Dict, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

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
DEFAULT_WEBHOOK_PATH = "/carbonvoice/webhook"
DEFAULT_PORT = 8645
DEFAULT_BIND_HOST = "0.0.0.0"
MESSAGE_POSTED_EVENT = "message.posted.to.channel"
MAX_DEDUPE_ENTRIES = 10_000
HTTP_TIMEOUT = 30.0


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off", ""}:
            return False
    return default


def _auth_headers(pat: str) -> Dict[str, str]:
    """Carbon Voice accepts PATs via Bearer auth and other keys via x-api-key."""
    trimmed = pat.strip()
    if trimmed.lower().startswith("cv_pat_"):
        return {"Authorization": f"Bearer {trimmed}"}
    return {"x-api-key": trimmed}


def _join_url(base: str, path: str) -> str:
    base_clean = base.rstrip("/")
    p = path if path.startswith("/") else "/" + path
    return f"{base_clean}{p}"


def _first_str(*vals: Any) -> Optional[str]:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def check_requirements() -> bool:
    if not HTTPX_AVAILABLE:
        logger.error("carbonvoice: httpx not installed")
        return False
    if not AIOHTTP_AVAILABLE:
        logger.error("carbonvoice: aiohttp not installed")
        return False
    return True


def validate_config(config: PlatformConfig) -> bool:
    """Return True when the config has the minimum required credentials."""
    extra = getattr(config, "extra", {}) or {}
    pat = os.getenv("CARBONVOICE_PAT") or config.token or extra.get("pat", "")
    public_base = (
        os.getenv("CARBONVOICE_PUBLIC_WEBHOOK_BASE_URL")
        or extra.get("public_webhook_base_url", "")
    )
    return bool(pat and public_base)


def is_connected(config: PlatformConfig) -> bool:
    extra = config.extra or {}
    return bool((config.token or extra.get("pat")) and extra.get("public_webhook_base_url"))


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed PlatformConfig.extra from env vars before adapter construction."""
    pat = os.getenv("CARBONVOICE_PAT")
    public_base = os.getenv("CARBONVOICE_PUBLIC_WEBHOOK_BASE_URL")
    if not pat or not public_base:
        return None

    seed: Dict[str, Any] = {
        "pat": pat,
        "public_webhook_base_url": public_base.rstrip("/"),
        "base_url": (os.getenv("CARBONVOICE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        "webhook_path": os.getenv("CARBONVOICE_WEBHOOK_PATH") or DEFAULT_WEBHOOK_PATH,
        "port": int(os.getenv("CARBONVOICE_PORT") or DEFAULT_PORT),
        "bind_host": os.getenv("CARBONVOICE_BIND_HOST") or DEFAULT_BIND_HOST,
        "creator_id": os.getenv("CARBONVOICE_CREATOR_ID") or None,
        "webhook_auth_header_name": os.getenv("CARBONVOICE_WEBHOOK_AUTH_HEADER_NAME") or None,
        "webhook_auth_header_value": os.getenv("CARBONVOICE_WEBHOOK_AUTH_HEADER_VALUE") or None,
    }

    home_channel_id = os.getenv("CARBONVOICE_HOME_CHANNEL")
    if home_channel_id:
        seed["home_channel"] = {
            "chat_id": home_channel_id,
            "name": os.getenv("CARBONVOICE_HOME_CHANNEL_NAME") or home_channel_id,
        }
    return seed


class CarbonVoiceAdapter(BasePlatformAdapter):
    """Hermes ↔ Carbon Voice bridge."""

    MAX_MESSAGE_LENGTH = 8000

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("carbonvoice"))
        extra = config.extra or {}
        self._pat: str = config.token or extra.get("pat") or ""
        self._base_url: str = (extra.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._public_base_url: str = (extra.get("public_webhook_base_url") or "").rstrip("/")
        self._webhook_path: str = extra.get("webhook_path") or DEFAULT_WEBHOOK_PATH
        self._port: int = int(extra.get("port") or DEFAULT_PORT)
        self._bind_host: str = extra.get("bind_host") or DEFAULT_BIND_HOST
        self._creator_id: Optional[str] = extra.get("creator_id") or None
        self._auth_header_name: Optional[str] = extra.get("webhook_auth_header_name") or None
        self._auth_header_value: Optional[str] = extra.get("webhook_auth_header_value") or None

        self._self_user_id: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._dedupe: set[str] = set()
        # Stash last seen incoming message_guid per channel for clean reply threading
        # when Hermes core calls send() without an explicit reply_to.
        self._last_inbound_msg: Dict[str, str] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        if not self._pat:
            logger.error("carbonvoice: CARBONVOICE_PAT not set")
            return False
        if not self._public_base_url:
            logger.error("carbonvoice: CARBONVOICE_PUBLIC_WEBHOOK_BASE_URL not set")
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

        public_webhook_url = _join_url(self._public_base_url, self._webhook_path)
        try:
            await self._subscribe(public_webhook_url)
        except Exception as exc:
            logger.error("carbonvoice: webhook subscribe failed: %s", exc)
            await self._close_client()
            return False

        try:
            await self._start_webhook_server()
        except Exception as exc:
            logger.error("carbonvoice: webhook server failed to start: %s", exc)
            await self._close_client()
            return False

        self._mark_connected()
        logger.info(
            "carbonvoice: connected as %s, listening on %s:%d%s, public webhook %s",
            self._self_user_id, self._bind_host, self._port, self._webhook_path, public_webhook_url,
        )
        return True

    async def disconnect(self) -> None:
        await self._stop_webhook_server()
        await self._close_client()
        self._mark_disconnected()

    async def _close_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ── Outbound (Hermes → Carbon Voice) ────────────────────────────────────

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

        # Thread the reply: explicit reply_to wins; otherwise use the last
        # inbound message_guid we saw for this channel.
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
        # Carbon Voice agent typing indicators are not part of the public REST surface.
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "carbonvoice", "chat_id": chat_id}

    # ── Carbon Voice REST helpers ───────────────────────────────────────────

    async def _whoami(self) -> Optional[str]:
        assert self._client is not None
        resp = await self._client.get("/whoami")
        resp.raise_for_status()
        data = resp.json() or {}
        user = data.get("user") or {}
        return _first_str(user.get("user_guid"), user.get("_id"), user.get("id"))

    async def _subscribe(self, webhook_url: str) -> None:
        assert self._client is not None
        filters = [{"key": "creator_id", "operator": "ne", "value": self._self_user_id}]
        if self._creator_id:
            filters.append({"key": "creator_id", "operator": "eq", "value": self._creator_id})
        body = {
            "subscriptions": [MESSAGE_POSTED_EVENT],
            "webhookURL": webhook_url,
            "subscription_filters": filters,
        }
        try:
            resp = await self._client.post("/apps/subscribe", json=body)
            resp.raise_for_status()
            logger.info("carbonvoice: subscribed webhook %s", webhook_url)
        except httpx.HTTPStatusError as exc:
            # Duplicate subscribe on restart returns 400 with body mentioning "same webhook url".
            body_text = exc.response.text.lower() if exc.response is not None else ""
            if exc.response is not None and exc.response.status_code == 400 and "same webhook url" in body_text:
                logger.info("carbonvoice: webhook already subscribed (%s)", webhook_url)
                return
            raise

    # ── Inbound webhook server ──────────────────────────────────────────────

    async def _start_webhook_server(self) -> None:
        self._app = web.Application()
        self._app.router.add_post(self._webhook_path, self._handle_webhook)
        self._app.router.add_get("/healthz", lambda _r: web.json_response({"ok": True}))
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._bind_host, self._port)
        await self._site.start()

    async def _stop_webhook_server(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
        self._app = None

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        if self._auth_header_name and self._auth_header_value:
            got = request.headers.get(self._auth_header_name)
            if not got or not hmac.compare_digest(got.strip(), self._auth_header_value.strip()):
                logger.warning("carbonvoice: webhook auth header mismatch")
                return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception as exc:
            logger.warning("carbonvoice: invalid webhook JSON: %s", exc)
            return web.json_response({"error": "invalid json"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"error": "invalid payload"}, status=400)

        if payload.get("eventName") != MESSAGE_POSTED_EVENT:
            return web.Response(status=204)

        data = payload.get("data") or {}
        resource = data.get("resource") or {}
        if not isinstance(resource, dict):
            return web.json_response({"error": "missing resource"}, status=400)

        channel_guid = _first_str(resource.get("channel_guid"), resource.get("channel_id"))
        message_guid = _first_str(resource.get("message_guid"), resource.get("message_id"))
        creator_guid = _first_str(resource.get("creator_guid"), resource.get("creator_id"))
        text = (_first_str(resource.get("transcript_txt"), resource.get("ai_summary_txt")) or "").strip()

        if not channel_guid or not message_guid or not creator_guid:
            logger.warning("carbonvoice: webhook missing channel/message/creator id")
            return web.json_response({"error": "missing identifiers"}, status=400)

        if not text:
            return web.Response(status=204)

        if message_guid in self._dedupe:
            return web.Response(status=204)
        self._dedupe.add(message_guid)
        if len(self._dedupe) > MAX_DEDUPE_ENTRIES:
            self._dedupe.clear()

        # Track last inbound for reply threading.
        self._last_inbound_msg[channel_guid] = message_guid

        parent_guid = _first_str(resource.get("parent_message_guid"), resource.get("parent_message_id"))
        reply_anchor = parent_guid or message_guid

        source = SessionSource(
            platform=Platform("carbonvoice"),
            chat_id=channel_guid,
            chat_name=f"cv:{channel_guid[:8]}",
            chat_type="dm",
            user_id=creator_guid,
            user_name=creator_guid,
            message_id=message_guid,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=message_guid,
            reply_to_message_id=reply_anchor if reply_anchor != message_guid else None,
        )

        # Return 204 immediately and process in the background — Carbon Voice
        # may otherwise retry while the agent is still thinking.
        asyncio.create_task(self._dispatch(event))
        return web.Response(status=204)

    async def _dispatch(self, event: MessageEvent) -> None:
        try:
            await self.handle_message(event)
        except Exception as exc:
            logger.exception("carbonvoice: dispatch failed: %s", exc)


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
    async with httpx.AsyncClient(base_url=base_url, headers=_auth_headers(pat), timeout=HTTP_TIMEOUT) as client:
        try:
            resp = await client.post("/v3/messages/start", json=body)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            return {"success": True, "message_id": _first_str(data.get("message_id"), data.get("id"))}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


def interactive_setup() -> Optional[Dict[str, str]]:
    """Lightweight wizard: gather PAT and public webhook base URL."""
    try:
        pat = input("Carbon Voice PAT (cv_pat_...): ").strip()
        public_base = input("Public webhook base URL (e.g. https://abc123.ngrok.io): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not pat or not public_base:
        return None
    return {
        "CARBONVOICE_PAT": pat,
        "CARBONVOICE_PUBLIC_WEBHOOK_BASE_URL": public_base,
    }


# ── Plugin entry point ─────────────────────────────────────────────────────

def register(ctx) -> None:
    """Called by the Hermes plugin system on discovery."""
    ctx.register_platform(
        name="carbonvoice",
        label="Carbon Voice",
        adapter_factory=lambda cfg: CarbonVoiceAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["CARBONVOICE_PAT", "CARBONVOICE_PUBLIC_WEBHOOK_BASE_URL"],
        install_hint="pip install httpx aiohttp  (usually already present in the Hermes venv)",
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
