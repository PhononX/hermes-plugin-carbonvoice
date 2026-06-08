"""Carbon Voice plugin defaults shared across modules."""

from __future__ import annotations

DEFAULT_BASE_URL = "https://api.carbonvoice.app"
DEFAULT_POLL_INTERVAL_MS = 5_000
DEFAULT_WS_RETRY_INITIAL_MS = 1_000
DEFAULT_WS_RETRY_MAX_MS = 30_000
DEFAULT_SEEN_TTL_S = 5 * 60
DEFAULT_FLUSH_DEBOUNCE_S = 5.0

# How long a message may stay "stuck" (no transcript yet) before we stop
# holding the cursor for it. CV usually finishes transcribing within
# seconds; a message with no transcript after this window almost certainly
# never will (image-only / system / failed STT). Past the cutoff we let it
# pass so it can't pin the cursor forever and re-feed the whole window on
# every poll/restart. Override with CARBONVOICE_STUCK_MAX_AGE_S.
DEFAULT_STUCK_MAX_AGE_S = 5 * 60
HTTP_TIMEOUT = 30.0
MAX_MESSAGE_LENGTH = 8000

# "acknowledged" is a built-in Carbon Voice reaction id — works out of the
# box without operator config. Override with CARBONVOICE_REACTION_ID after
# inspecting the available reactions logged on startup.
DEFAULT_REACTION_ID = "acknowledged"

# "confused" (⁉️) is a built-in CV reaction. We put it on an unauthorized
# sender's first message as a silent "we saw you, you're pending approval"
# signal — instead of posting a text reply that clutters the channel and
# spams every old conversation when deny-by-default re-flags them. Override
# with CARBONVOICE_PENDING_REACTION_ID.
DEFAULT_PENDING_REACTION_ID = "confused"
