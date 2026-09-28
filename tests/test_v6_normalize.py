"""``normalize_v6``: MessageV6 → the legacy shape the parse path reads.

REST reads now use cv-api's ``/v6`` message routes while Socket.IO events
still deliver the legacy (v2/v3) shape. Both flow through the same
``extract_*`` helpers, so the v6 payload is mapped back to legacy once, at the
fetch boundary. These tests lock the field mapping and prove a legacy socket
payload and its v6 twin parse identically.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent  # repo root (package dir)


def _load_parse():
    pkg_name = "carbonvoice"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(ROOT)]
        sys.modules[pkg_name] = pkg
    for name in ("constants", "parse"):
        mod_name = f"{pkg_name}.{name}"
        if mod_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(mod_name, ROOT / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[f"{pkg_name}.parse"]


parse = _load_parse()
normalize_v6 = parse.normalize_v6


def _v6(**overrides):
    base = {
        "id": "M1",
        "name": "Standup notes",
        "type": "channel",
        "kind": "audio",
        "created_at": "2026-09-24T10:00:00.000Z",
        "updated_at": "2026-09-24T10:00:12.000Z",
        "conversation_id": "C1",
        "workspace_id": "W1",
        "creator_id": "U1",
        "status": "active",
        "thread_id": "M1",
        "tagged_user_ids": ["BOT"],
        "reaction_summary": {"top_user_reactions": [
            {"user_id": "BOT", "reaction_id": "acknowledged"}]},
        "share_link_id": "SL1",
        "content": {
            "id": "A1",
            "transcript": "hello world",
            "ai_summary": "a greeting",
            "time_codes": [{"t": "hello", "s": 0, "e": 200}, {"t": "world", "s": 200, "e": 400}],
            "language": "english",
            "url": "https://s3/audio.mp3",
            "streaming_url": "https://stream/live.m3u8",
            "duration_ms": 1234,
            "waveform_percentage": "0hz",
        },
        "attachments": [{
            "id": "ATT1", "url": "https://s3/foo.png", "filename": "foo.png",
            "mime_type": "image/png", "type": "file", "status": "Uploaded",
            "length_in_bytes": 42, "creator_id": "U1",
        }],
    }
    base.update(overrides)
    return base


def test_core_field_mapping():
    out = normalize_v6(_v6())
    assert out["message_id"] == "M1" and out["id"] == "M1"
    assert out["channel_ids"] == ["C1"]
    assert out["workspace_ids"] == ["W1"]
    assert out["last_updated_at"] == "2026-09-24T10:00:12.000Z"
    assert out["creator_id"] == "U1"
    assert out["created_at"] == "2026-09-24T10:00:00.000Z"
    # Shape-identical fields pass through untouched.
    assert out["tagged_user_ids"] == ["BOT"]
    assert out["share_link_id"] == "SL1"
    assert out["name"] == "Standup notes"  # MessageV6 carries name (cv-api #483)
    assert out["reaction_summary"]["top_user_reactions"][0]["user_id"] == "BOT"
    assert out["is_text_message"] is False  # kind=audio
    # Renamed v6 keys must not leak through (esp. thread_id == id).
    for k in ("content", "conversation_id", "workspace_id", "thread_id", "updated_at"):
        assert k not in out, k


def test_reply_detection_uses_thread_id_only_when_it_differs_from_id():
    top = normalize_v6(_v6(thread_id="M1"))
    assert "parent_message_id" not in top
    assert parse.extract_reply_anchor(top) == "M1"

    reply = normalize_v6(_v6(id="M2", thread_id="ROOT"))
    assert reply["parent_message_id"] == "ROOT"
    assert parse.extract_reply_anchor(reply) == "ROOT"


def test_transcript_and_summary_text_models():
    out = normalize_v6(_v6())
    by_type = {m["type"]: m["value"] for m in out["text_models"]}
    assert by_type == {"transcript": "hello world", "summary": "a greeting"}
    assert parse.extract_transcript(out) == "hello world"


def test_transcript_falls_back_to_joined_time_codes():
    content = dict(_v6()["content"], transcript=None)
    out = normalize_v6(_v6(content=content))
    assert parse.extract_transcript(out) == "hello world"

    content = dict(_v6()["content"], transcript="   ", time_codes=[{"t": "only"}, {"s": 1}])
    assert parse.extract_transcript(normalize_v6(_v6(content=content))) == "only"


def test_audio_prefers_presigned_url_and_never_uses_streaming_url():
    out = normalize_v6(_v6())
    assert len(out["audio_models"]) == 1
    audio = out["audio_models"][0]
    assert audio["url"] == "https://s3/audio.mp3"
    assert audio["duration_ms"] == 1234 and audio["_id"] == "A1"
    assert audio["waveform_percentages"] == [0.0, int("h", 36) / 35, 1.0]

    presigned = dict(_v6()["content"], presigned_url="https://signed/audio.mp3")
    assert normalize_v6(_v6(content=presigned))["audio_models"][0]["url"] == \
        "https://signed/audio.mp3"

    live_only = {"streaming_url": "https://stream/live.m3u8", "transcript": "live"}
    assert normalize_v6(_v6(content=live_only))["audio_models"] == []


def test_attachments_map_id_url_to_legacy_names():
    out = normalize_v6(_v6())
    att = out["attachments"][0]
    assert att["_id"] == "ATT1" and att["link"] == "https://s3/foo.png"
    assert "id" not in att and "url" not in att
    assert att["filename"] == "foo.png" and att["status"] == "Uploaded"

    extracted = parse.extract_attachments(out)
    assert extracted == [{
        "_id": "ATT1", "link": "https://s3/foo.png", "filename": "foo.png",
        "mime_type": "image/png", "length_in_bytes": 42, "type": "file",
        "status": "Uploaded",
    }]


def test_missing_content_and_optional_fields():
    msg = _v6()
    for k in ("content", "attachments", "conversation_id", "kind", "updated_at"):
        msg.pop(k)
    out = normalize_v6(msg)
    assert out["text_models"] == [] and out["audio_models"] == []
    assert out["attachments"] == []
    assert out["channel_ids"] == []
    assert "is_text_message" not in out
    assert "last_updated_at" not in out
    assert parse.extract_transcript(out) == ""
    assert parse.extract_channel_id(out) is None

    assert normalize_v6(_v6(kind="text"))["is_text_message"] is True
    assert normalize_v6(None) == {} and normalize_v6(["x"]) == {}


def test_legacy_socket_payload_parses_identically_to_its_v6_twin():
    """The same message delivered by the socket (legacy) and fetched via v6
    (normalised) must look the same to every parse helper."""
    legacy = {
        "message_id": "M2",
        "creator_id": "U1",
        "created_at": "2026-09-24T10:00:00.000Z",
        "last_updated_at": "2026-09-24T10:00:12.000Z",
        "channel_ids": ["C1"],
        "workspace_ids": ["W1"],
        "parent_message_id": "ROOT",
        "is_text_message": False,
        "share_link_id": "SL1",
        "tagged_user_ids": ["BOT"],
        "text_models": [{"type": "transcript", "value": "hello world"}],
        "audio_models": [{"_id": "A1", "url": "https://s3/audio.mp3"}],
        "attachments": [{
            "_id": "ATT1", "link": "https://s3/foo.png", "filename": "foo.png",
            "mime_type": "image/png", "type": "file", "status": "Uploaded",
            "length_in_bytes": 42,
        }],
        "reaction_summary": {"top_user_reactions": []},
    }
    v6 = normalize_v6(_v6(
        id="M2", thread_id="ROOT",
        reaction_summary={"top_user_reactions": []},
    ))
    for fn in (
        parse.extract_message_id, parse.extract_channel_id,
        parse.extract_creator_id, parse.extract_transcript,
        parse.extract_reply_anchor, parse.extract_share_link_id,
        parse.extract_attachments,
    ):
        assert fn(legacy) == fn(v6), fn.__name__
    assert parse.is_user_mentioned(legacy, "BOT") == parse.is_user_mentioned(v6, "BOT")
    assert legacy["is_text_message"] == v6["is_text_message"]
    # The legacy socket payload itself is untouched by the new code path.
    assert parse.extract_transcript(legacy) == "hello world"
    assert parse.extract_channel_id(legacy) == "C1"
