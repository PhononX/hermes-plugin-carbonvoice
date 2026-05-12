"""Hermes plugin registration entry points.

Everything Hermes calls during plugin discovery lives here:

    check_requirements   — verify import-time deps (httpx mandatory, socketio optional)
    validate_config      — runtime check that PAT is present
    is_connected         — quick "is this plugin usable?" probe
    _env_enablement      — seed PlatformConfig.extra from environment
    interactive_setup    — terminal wizard for ``hermes setup``
    register             — wire everything into the gateway plugin registry
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from gateway.config import PlatformConfig

from .adapter import CarbonVoiceAdapter
from .api import standalone_send
from .constants import DEFAULT_BASE_URL, DEFAULT_POLL_INTERVAL_MS, DEFAULT_WS_RETRY_MAX_MS

logger = logging.getLogger(__name__)

try:
    import httpx  # noqa: F401
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    import socketio  # noqa: F401
    SOCKETIO_AVAILABLE = True
except ImportError:
    SOCKETIO_AVAILABLE = False


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
    extra = getattr(config, "extra", {}) or {}
    pat = os.getenv("CARBONVOICE_PAT") or config.token or extra.get("pat", "")
    return bool(pat)


def is_connected(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(config.token or extra.get("pat"))


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed PlatformConfig.extra from env vars before adapter construction.

    Returns a *flat* dict — Hermes core merges this into ``extra`` via
    ``config.platforms[platform].extra.update(seed)``, so nesting here would
    end up as ``extra["extra"]`` and the keys would never reach the adapter.
    """
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


def interactive_setup() -> Optional[Dict[str, str]]:
    """Lightweight wizard: gather PAT."""
    try:
        pat = input("Carbon Voice PAT (cv_pat_...): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not pat:
        return None
    return {"CARBONVOICE_PAT": pat}


async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    content: str,
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Adapter for Hermes' cron delivery hook — unwraps PlatformConfig and calls api.standalone_send."""
    extra = pconfig.extra or {}
    pat = pconfig.token or extra.get("pat")
    base_url = (extra.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    if not pat:
        return {"success": False, "error": "missing CARBONVOICE_PAT"}
    return await standalone_send(pat, base_url, chat_id, content)


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
