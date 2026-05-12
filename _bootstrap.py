"""Best-effort auto-install of the optional Socket.IO client.

Runs once when Hermes loads the plugin. If ``python-socketio`` is already
importable, this is a fast no-op. If it's missing, we attempt to install
it into the same interpreter Hermes is running:

  1. ``python -m ensurepip --upgrade`` — Hermes' uv-managed venv ships
     without pip; this bootstraps it.
  2. ``python -m pip install 'python-socketio[asyncio_client]'``

Both calls use ``sys.executable``, so the install lands in Hermes' venv
regardless of which Python the user invokes from outside.

Failures (offline, sandboxed venv, etc.) are logged once and the plugin
continues in REST-polling mode. The user can still install manually.
"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


def ensure_socketio() -> None:
    try:
        import socketio  # noqa: F401
        return
    except ImportError:
        pass

    logger.info(
        "carbonvoice: python-socketio missing — auto-installing for real-time delivery"
    )
    try:
        # 1. Bootstrap pip if absent (Hermes' uv-managed venv ships without it).
        subprocess.run(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            check=True,
            capture_output=True,
            timeout=60,
        )
        # 2. Install the actual dep.
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "python-socketio[asyncio_client]"],
            check=True,
            capture_output=True,
            timeout=180,
        )
        logger.info("carbonvoice: python-socketio installed ✓")
    except Exception as exc:
        logger.warning(
            "carbonvoice: could not auto-install python-socketio (%s) — "
            "falling back to polling-only mode. Install manually with: "
            "%s -m pip install 'python-socketio[asyncio_client]'",
            exc, sys.executable,
        )
