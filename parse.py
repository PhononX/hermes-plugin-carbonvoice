"""Pure parsing helpers for Carbon Voice payloads.

No I/O, no state, no async — everything here is a deterministic function
of the input dict. Keeps the rest of the plugin free of payload-shape knowledge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def auth_headers(pat: str) -> Dict[str, str]:
    """Carbon Voice accepts PATs via Bearer auth and other keys via x-api-key."""
    trimmed = pat.strip()
    if trimmed.lower().startswith("cv_pat_"):
        return {"Authorization": f"Bearer {trimmed}"}
    return {"x-api-key": trimmed}


def first_str(*vals: Any) -> Optional[str]:
    """Return the first non-empty string in *vals*, or None."""
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_transcript(msg: Dict[str, Any]) -> str:
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
    fallback = first_str(msg.get("transcript_txt"), msg.get("ai_summary_txt"))
    return fallback or ""


def extract_message_id(msg: Dict[str, Any]) -> Optional[str]:
    return first_str(msg.get("message_id"), msg.get("_id"))


def extract_channel_id(msg: Dict[str, Any]) -> Optional[str]:
    channel_ids = msg.get("channel_ids")
    if isinstance(channel_ids, list) and channel_ids:
        first = channel_ids[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return first_str(msg.get("channel_id"), msg.get("channel_guid"))


def extract_creator_id(msg: Dict[str, Any]) -> Optional[str]:
    return first_str(msg.get("creator_id"), msg.get("creator_guid"))


def extract_reply_anchor(msg: Dict[str, Any]) -> Optional[str]:
    """The message_id to thread *next* replies under.

    Carbon Voice rejects ``reply_to_message_id`` that points at a reply (it
    returns ``400 You cannot reply to a message that is a reply``). To stay
    safe, we anchor to the parent of the inbound message when present, and
    fall back to the message itself only when it is a top-level message.
    Mirrors the ``parent_message_id ?? message_id`` pattern in the TypeScript
    reference implementation.
    """
    parent = first_str(
        msg.get("parent_message_id"), msg.get("parent_message_guid")
    )
    return parent or extract_message_id(msg)
