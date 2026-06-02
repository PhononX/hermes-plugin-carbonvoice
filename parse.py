"""Pure parsing helpers for Carbon Voice payloads.

No I/O, no state, no async — everything here is a deterministic function
of the input dict. Keeps the rest of the plugin free of payload-shape knowledge.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Carbon Voice embeds mentions inline in the transcript as ``@[Display
# Name](user_guid)`` — markdown-link-style syntax produced by the Flutter
# client when a user tags someone. The display name can include any
# character except ``]``; the guid is opaque alphanumerics but we accept
# anything that isn't ``)`` to stay forgiving of future format tweaks.
_INLINE_MENTION_PATTERN = re.compile(r"@\[([^\]]+)\]\(([^)]+)\)")


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

    Shape compatibility — checked in order so the V5 source-of-truth
    payload wins, with the older shapes kept as fallback for the brief
    window between socket signal and the v5 GET enrichment (and for
    webhook callers that haven't migrated yet):

      - **V5 / GET ``/v5/messages/:id``**: top-level ``transcript`` string.
      - **V2 (socket push, ``/v3/messages/recent``)**: ``text_models[]``
        with one entry of ``type == "transcript"`` carrying either a
        joined ``timecodes[].t`` walk or a ``value`` string.
      - **Webhook**: ``transcript_txt`` or ``ai_summary_txt`` flat
        strings.

    When the message is still being transcribed all of these are empty;
    callers must treat an empty return as "not ready yet" and retry.
    """
    # V5 — preferred. Single source of truth per cv-api design.
    v5_transcript = msg.get("transcript")
    if isinstance(v5_transcript, str) and v5_transcript.strip():
        return v5_transcript.strip()
    # V2 — socket / v3-poll fallback.
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
    # V5 uses ``id``; V2 uses ``message_id``; legacy uses ``_id``.
    return first_str(msg.get("id"), msg.get("message_id"), msg.get("_id"))


def extract_channel_id(msg: Dict[str, Any]) -> Optional[str]:
    # V5 uses ``conversation_id`` (singular); V2 uses ``channel_ids[0]``;
    # webhook payloads use ``channel_id`` / ``channel_guid``.
    v5 = first_str(msg.get("conversation_id"))
    if v5:
        return v5
    channel_ids = msg.get("channel_ids")
    if isinstance(channel_ids, list) and channel_ids:
        first = channel_ids[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return first_str(msg.get("channel_id"), msg.get("channel_guid"))


def extract_creator_id(msg: Dict[str, Any]) -> Optional[str]:
    # Same field across V2 and V5.
    return first_str(msg.get("creator_id"), msg.get("creator_guid"))


def extract_attachments(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return normalized inbound attachments as a list of dicts.

    Walks ``msg['attachments']`` and returns one dict per attachment
    with these keys (mirrors the field names CV uses on the wire):

        - ``_id``: server-assigned attachment id (used by
          :meth:`CarbonVoiceAPI.get_attachment_download_url` to resolve
          a pre-signed S3 GET URL)
        - ``link``: canonical S3 URL (auth-gated — don't try to GET it
          without going through the signedurl endpoint first)
        - ``filename``: as uploaded; often a UUID, not a friendly name
        - ``mime_type``: e.g. ``"image/png"``, ``"application/pdf"``
        - ``length_in_bytes``: int or None (CV sometimes leaves it null)
        - ``type``: typically ``"file"``; other AttachmentType values
          (``link``, ``location``, ...) are rare on inbound

    Entries missing both ``_id`` and ``link`` are dropped (defensive —
    CV's responses occasionally include legacy/null rows). Voice memos
    are NOT included here; their audio + transcript live in
    ``audio_models[]`` and ``text_models[]`` respectively, surfaced via
    :func:`extract_transcript` and inbound media handling on the audio
    side (separate path, future PR).
    """
    out: List[Dict[str, Any]] = []
    for att in (msg.get("attachments") or []):
        if not isinstance(att, dict):
            continue
        aid = first_str(att.get("_id"), att.get("id"))
        link = first_str(att.get("link"), att.get("url"))
        if not aid and not link:
            continue
        out.append({
            "_id": aid or "",
            "link": link or "",
            "filename": att.get("filename") or "",
            "mime_type": att.get("mime_type") or "",
            "length_in_bytes": att.get("length_in_bytes"),
            "type": att.get("type") or "file",
        })
    return out


def extract_inline_mentions(text: Optional[str]) -> List[Tuple[str, str]]:
    """Return ``(display_name, user_guid)`` pairs from CV's inline syntax.

    Carbon Voice's Flutter app emits mentions as ``@[Display Name](guid)``
    in the message transcript. This helper extracts each pair in order of
    appearance, without de-duplication (so the caller can count repeats
    if it ever matters).
    """
    if not text:
        return []
    return [
        (m.group(1), m.group(2))
        for m in _INLINE_MENTION_PATTERN.finditer(text)
    ]


def is_user_mentioned(msg: Dict[str, Any], user_id: Optional[str]) -> bool:
    """Return True when *user_id* is tagged in *msg*.

    Forward-compatible: prefers a structured ``tagged_user_ids`` field
    when the API exposes it (pending cv-api PR), and falls back to
    parsing the inline ``@[name](guid)`` syntax in the transcript. The
    fallback works today; the structured path will take over
    automatically once the backend ships the field.
    """
    if not user_id:
        return False
    tagged = msg.get("tagged_user_ids")
    if isinstance(tagged, list) and tagged:
        return user_id in tagged
    return any(
        guid == user_id
        for _, guid in extract_inline_mentions(extract_transcript(msg))
    )


def strip_inline_mentions(text: Optional[str]) -> str:
    """Replace ``@[name](guid)`` with a bare ``@name`` for cleaner agent input.

    The agent doesn't need to see the guid — it adds noise to the LLM
    prompt and can confuse instruction-following. The bare ``@name`` is
    what the agent should see; the original guid stays out of the model
    context.
    """
    if not text:
        return text or ""
    return _INLINE_MENTION_PATTERN.sub(r"@\1", text)


def chat_type_from_channel(channel: Optional[Dict[str, Any]]) -> str:
    """Map a Carbon Voice channel payload to Hermes ``chat_type``.

    Returns ``"dm"`` for one-to-one direct messages, ``"group"`` for every
    other channel kind (workspace channels, customer conversations, async
    meetings). Defaults to ``"dm"`` when the payload is missing so the
    adapter degrades to the prior single-tier behavior rather than dropping
    messages on a transient channel-lookup failure.

    Discriminator priority:
      1. ``type == "directMessage"`` — explicit type from PersonalizedChannel.
      2. ``dm_hash`` non-null — present only on DM channels (1:1 fingerprint
         used by the merge service); a reliable fallback if ``type`` is
         absent from older payloads.
    """
    if not channel:
        return "dm"
    ch_type = channel.get("type")
    if isinstance(ch_type, str) and ch_type.strip():
        return "dm" if ch_type == "directMessage" else "group"
    if channel.get("dm_hash"):
        return "dm"
    # Unknown/partial payload — preserve the prior "bot responds always"
    # behavior by defaulting to DM until we gain a positive signal.
    return "dm"


def extract_reply_anchor(msg: Dict[str, Any]) -> Optional[str]:
    """The message_id to thread *next* replies under.

    Resolves to ``parent_message_id`` (the thread root) when the inbound
    message is a reply, else the message's own id. Mirrors the
    ``parent_message_id ?? message_id`` pattern in the TypeScript client.

    As of cv-api PR #277 (CV-13155) the backend resolves the thread root
    server-side (``resolveRootParentMessageId``): sending a non-root id as
    ``reply_to_message_id`` no longer returns ``400 You cannot reply to a
    message that is a reply`` — it is normalized to the root. The only
    remaining reply error is cross-conversation. Anchoring to the root
    here is therefore belt-and-suspenders, not a hard requirement.
    """
    parent = first_str(
        msg.get("parent_message_id"), msg.get("parent_message_guid")
    )
    return parent or extract_message_id(msg)
