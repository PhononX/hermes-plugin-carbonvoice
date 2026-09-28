"""v6 read endpoints in ``CarbonVoiceAPI``, exercised over ``httpx.MockTransport``.

Each read must hit the ``/v6`` route with the contract's request shape and
hand callers a payload already normalised to the legacy shape (see
``parse.normalize_v6``), so the rest of the plugin is shape-agnostic.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent  # repo root (package dir)


def _load(*names):
    pkg_name = "carbonvoice"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(ROOT)]
        sys.modules[pkg_name] = pkg
    for name in names:
        mod_name = f"{pkg_name}.{name}"
        if mod_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(mod_name, ROOT / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[f"{pkg_name}.api"]


api_mod = _load("constants", "parse", "api")
CarbonVoiceAPI = api_mod.CarbonVoiceAPI


class Recorder:
    """MockTransport handler: records requests, answers from a route table."""

    def __init__(self, routes):
        self.routes = routes  # {(method, path): response | callable(request)}
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes[(request.method, request.url.path)]
        return handler(request) if callable(handler) else handler


def make_api(routes):
    rec = Recorder(routes)
    api = CarbonVoiceAPI("cv_pat_test", "https://cv.test")
    api._client = httpx.AsyncClient(
        base_url="https://cv.test", transport=httpx.MockTransport(rec)
    )
    return api, rec


V6_MSG = {
    "id": "M1", "type": "channel", "kind": "text", "creator_id": "U1",
    "created_at": "2026-09-24T10:00:00Z", "updated_at": "2026-09-24T10:00:05Z",
    "conversation_id": "C1", "workspace_id": "W1", "status": "active",
    "thread_id": "ROOT", "tagged_user_ids": ["BOT"], "name": "Title",
    "content": {"transcript": "hi there"},
    "attachments": [{"id": "A1", "url": "https://s3/a.png", "mime_type": "image/png"}],
}


def test_get_message_v6_unwraps_envelope_and_normalises():
    api, rec = make_api({
        ("GET", "/v6/messages/M1"): httpx.Response(200, json={"message": V6_MSG}),
    })
    out = asyncio.run(api.get_message_v6("M1"))
    assert rec.requests[0].url.path == "/v6/messages/M1"
    assert out["message_id"] == "M1"
    assert out["channel_ids"] == ["C1"]
    assert out["parent_message_id"] == "ROOT"
    assert out["tagged_user_ids"] == ["BOT"]
    assert out["name"] == "Title"
    assert out["attachments"][0]["_id"] == "A1"
    assert out["text_models"][0]["value"] == "hi there"


def test_get_message_v6_optional_query_and_4xx():
    api, rec = make_api({
        ("GET", "/v6/messages/M1"): httpx.Response(200, json={"message": V6_MSG}),
        ("GET", "/v6/messages/GONE"): httpx.Response(404, json={"message": "nope"}),
    })
    asyncio.run(api.get_message_v6("M1", language="spanish", presigned_url=True, fresh=True))
    params = dict(rec.requests[0].url.params)
    assert params == {"language": "spanish", "presigned_url": "true", "fresh": "true"}
    assert asyncio.run(api.get_message_v6("GONE")) is None


def test_get_messages_by_ids_v6_body_and_bare_array_response():
    api, rec = make_api({
        ("POST", "/v6/messages/by-ids"): httpx.Response(200, json=[V6_MSG]),
    })
    out = asyncio.run(api.get_messages_by_ids_v6(" C1 ", ["M1"]))
    body = json.loads(rec.requests[0].content)
    assert body == {"conversation_id": "C1", "message_ids": ["M1"]}
    assert [m["message_id"] for m in out] == ["M1"]
    assert out[0]["channel_ids"] == ["C1"]
    # Empty id list short-circuits (the endpoint 400s on an empty list).
    assert asyncio.run(api.get_messages_by_ids_v6("C1", [])) == []
    assert len(rec.requests) == 1


def test_get_share_link_v6_normalises_shared_message():
    api, rec = make_api({
        ("GET", "/v6/message-sharelinks/SL1"): httpx.Response(200, json={
            "id": "SL1", "has_channel_access": False, "shared_message": V6_MSG,
        }),
        ("GET", "/v6/message-sharelinks/REVOKED"): httpx.Response(403),
    })
    out = asyncio.run(api.get_share_link("SL1"))
    assert rec.requests[0].url.path == "/v6/message-sharelinks/SL1"
    assert out["id"] == "SL1" and out["has_channel_access"] is False
    shared = out["shared_message"]
    assert shared["creator_id"] == "U1"
    assert shared["attachments"][0] == {
        "mime_type": "image/png", "_id": "A1", "link": "https://s3/a.png",
    }
    assert shared["text_models"][0]["value"] == "hi there"
    assert asyncio.run(api.get_share_link("REVOKED")) is None
