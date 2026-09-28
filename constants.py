"""Carbon Voice plugin defaults shared across modules."""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_BASE_URL = "https://api.carbonvoice.app"
DEFAULT_POLL_INTERVAL_MS = 5_000
DEFAULT_WS_RETRY_INITIAL_MS = 1_000
DEFAULT_WS_RETRY_MAX_MS = 30_000
DEFAULT_SEEN_TTL_S = 5 * 60
DEFAULT_FLUSH_DEBOUNCE_S = 5.0

# How long a gate-rejected *voice* message in a group may stay revisit-held
# (cursor pinned, re-evaluated each tick) waiting for its picker tags.
# Flutter applies tags via the batch PUT only after STT (~10–30s after
# create); within this window a "no mention" verdict is provisional. Text
# messages carry tags on the create body and never hold. Override with
# CARBONVOICE_REVISIT_MAX_AGE_S.
DEFAULT_REVISIT_MAX_AGE_S = 90

# Delay before the one-shot self-scheduled re-tick that retries stuck /
# revisit-held messages. Keeps retries flowing in WS mode (where polling is
# stopped) without waiting for the next unrelated socket event.
STUCK_RETRY_DELAY_S = 6.0

# How long a message may stay "stuck" (no transcript yet) before we stop
# holding the cursor for it. CV usually finishes transcribing within
# seconds; a message with no transcript after this window almost certainly
# never will (image-only / system / failed STT). Past the cutoff we let it
# pass so it can't pin the cursor forever and re-feed the whole window on
# every poll/restart. Override with CARBONVOICE_STUCK_MAX_AGE_S.
DEFAULT_STUCK_MAX_AGE_S = 5 * 60

# GET /v6/messages/updates paging. 200 is the server's default and maximum
# page size. A single sync follows ``next_cursor`` for at most
# UPDATES_MAX_PAGES_PER_TICK pages; if the feed still ``has_more`` after
# that, the reached cursor is persisted and a trailing re-fetch continues
# from it, so a long backlog can't monopolize one tick.
UPDATES_PAGE_LIMIT = 200
UPDATES_MAX_PAGES_PER_TICK = 25

# The updates feed may re-deliver up to ~4s of already-seen rows behind a
# resume cursor (and the first date-anchored page is shifted back ~4s).
# The poller remembers this many (id → last_updated_at) pairs to drop those
# re-deliveries; a genuine update carries a new last_updated_at and passes.
POLL_DEDUPE_MAX = 2_000
HTTP_TIMEOUT = 30.0
MAX_MESSAGE_LENGTH = 8000

# Request-source headers so the backend can categorize traffic per client
# (mirrors the Flutter app's lowercase-hyphenated headers like ``platform``
# and ``mobile-app-version``). ``agent-name`` is static — it identifies the
# integration type (hermes vs openclaw vs cloud-channel vs the apps).
# ``agent-id`` is dynamic — the bot account's user_guid from /whoami,
# injected once known so traffic can also be grouped per agent account.
AGENT_NAME_HEADER = "agent-name"
AGENT_NAME_VALUE = "hermes"
AGENT_ID_HEADER = "agent-id"


def _plugin_version() -> str:
    try:
        text = (Path(__file__).parent / "plugin.yaml").read_text()
        match = re.search(r"^version:\s*(\S+)", text, re.MULTILINE)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "unknown"


# The backend's request logger only captures a fixed header set (ua,
# mobile-app-version, platform), so the User-Agent is what actually lets it
# categorize Hermes traffic today — agent-name/agent-id above are sent for
# when the backend starts logging them. After /whoami the api client appends
# " (agent-id: <guid>)" so the ua field also distinguishes agent accounts.
USER_AGENT = f"hermes-plugin/{_plugin_version()}"

# Carbon Voice's API gateway intermittently returns 502/503/504 (observed in
# bursts). A transient 5xx on a latency-critical GET/reaction would otherwise
# wait for the next poll tick (~5s) to recover; a couple of fast retries with
# short backoff recover in well under a second. Only idempotent reads/reactions
# retry — sends do NOT (a retried send could duplicate a delivered message).
TRANSIENT_RETRY_ATTEMPTS = 2
TRANSIENT_RETRY_BACKOFF_S = 0.4
TRANSIENT_STATUS = (502, 503, 504)

# ── Activity signals ────────────────────────────────────────────────────
# POST /v5/conversations/:id/signal broadcasts an ephemeral "the agent is
# working" indicator to clients viewing the conversation (cv-api CV-13490).
# It is stateless server-side: a signal must be re-posted every ~1-2s to stay
# visible and simply stops being sent to clear it. Hermes core drives the
# heartbeat via its ~2s send_typing loop.
SIGNAL_TTL_MS = 4_000            # client-side expiry hint; kept above the ~2s
                                 # re-post cadence so one skipped beat doesn't
                                 # drop the indicator
SIGNAL_CLEAR_TTL_MS = 500        # ttl on the turn-ending clear beat. cv-api
                                 # clamps ttl_ms to [500, 30000] and the signal
                                 # endpoint has no delete verb, so the smallest
                                 # legal lease IS the clear: re-asserting the
                                 # current phase with this ttl on turn end drops
                                 # the dot within ~1s instead of waiting out the
                                 # full SIGNAL_TTL_MS lease (up to ~4s after the
                                 # reply is already visible). Mirrors the
                                 # cv-agents ActivityLease.stop() clear.
SIGNAL_BODY_MAX = 200            # server rejects a body over 200 chars (400)
SIGNAL_REQUEST_TIMEOUT_S = 2.0   # short: a slow signal POST must not back up
                                 # the ~2s typing loop
SIGNAL_ERROR_COOLDOWN_S = 10.0   # CV's gateway bursts 502s; after a failure,
                                 # pause signals for that conversation instead
                                 # of hammering every ~2s (mirrors the upstream
                                 # Signal adapter's typing backoff)

# Map Hermes core's status_callback event kinds → the CV ConversationSignalType
# enum (typing | thinking | tool_call | searching | processing). Unmapped kinds
# fall back to SIGNAL_TYPE_DEFAULT — lifecycle/context-pressure notices read as
# generic "processing".
SIGNAL_TYPE_BY_KIND = {
    "thinking": "thinking",
    "_thinking": "thinking",
    "reasoning": "thinking",
    "reasoning.available": "thinking",
    "tool": "tool_call",
    "tool.started": "tool_call",
    "tool_call": "tool_call",
    "search": "searching",
    "searching": "searching",
    "lifecycle": "processing",
    "session:compress": "processing",
    "status": "processing",
}
SIGNAL_TYPE_DEFAULT = "processing"


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

# One-tap owner approval: instead of copying "/cv-allow-user <id>", the owner
# just reacts on the bot's "X wants to talk to me" prompt — 💯 to allow, 👎 to
# block. Mirrors cv-claude-channels' reaction-based permission relay. These
# are CV built-in reaction *ids* (the id is what counts; CV stores the
# "negative" reaction with code ⛔ but clients render it as a thumbs-down 👎);
# override via CARBONVOICE_APPROVE_REACTION_ID / CARBONVOICE_REJECT_REACTION_ID.
DEFAULT_APPROVE_REACTION_ID = "affirmative"  # 💯
DEFAULT_REJECT_REACTION_ID = "negative"      # 👎 (stored code ⛔)
