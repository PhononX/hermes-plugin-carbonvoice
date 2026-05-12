from ._bootstrap import ensure_socketio

# Auto-install python-socketio if missing — runs once per environment,
# no-op on subsequent loads. Must happen before .setup imports run because
# transport.py snapshots SOCKETIO_AVAILABLE at module load.
ensure_socketio()

from .setup import register

__all__ = ["register"]
