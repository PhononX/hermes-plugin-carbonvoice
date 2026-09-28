"""Poll fallback over ``GET /v6/messages/updates`` (cursor paging).

Covers the request contract of ``CarbonVoiceAPI.fetch_message_updates`` (over
``httpx.MockTransport``) and the adapter's sync loop
(``_fetch_missed_messages_locked``) against a scripted fake API:

- follows ``next_cursor`` while ``has_more`` and persists the tail cursor,
  only after the whole batch was handled;
- drops the feed's ~4s re-deliveries (same id + last_updated_at) but still
  processes a genuine update;
- a 400 on the stored cursor clears it and re-anchors by date; any other
  failure keeps it;
- a stuck message holds the cursor at the page that delivered it.

``adapter.py`` imports ``gateway.*`` (only present inside an installed
Hermes), so — like the CI smoke test — the gateway modules are stubbed and
the adapter is built via ``object.__new__``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types
from collections import OrderedDict

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent  # repo root (package dir)


def _bootstrap():
    # --- Stub gateway.* so adapter.py imports without an installed Hermes. ---
    class _Named:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class SendResult:
        def __init__(self, success=False, message_id=None, error=None, **kw):
            self.success = success
            self.message_id = message_id
            self.error = error
            self.__dict__.update(kw)

    class BasePlatformAdapter:  # minimal base; adapter is built via __new__
        pass

    gw = types.ModuleType("gateway")
    gw.__path__ = []  # mark as package
    gw_config = types.ModuleType("gateway.config")
    gw_config.Platform = _Named
    gw_config.PlatformConfig = _Named
    gw_platforms = types.ModuleType("gateway.platforms")
    gw_platforms.__path__ = []
    gw_base = types.ModuleType("gateway.platforms.base")
    gw_base.BasePlatformAdapter = BasePlatformAdapter
    gw_base.MessageEvent = _Named
    gw_base.MessageType = types.SimpleNamespace(TEXT="text", VOICE="voice")
    gw_base.SendResult = SendResult
    gw_base.IMAGE_CACHE_DIR = str(ROOT)
    gw_session = types.ModuleType("gateway.session")
    gw_session.SessionSource = _Named
    gw_session.build_session_key = lambda *a, **k: "sk"
    for name, mod in {
        "gateway": gw,
        "gateway.config": gw_config,
        "gateway.platforms": gw_platforms,
        "gateway.platforms.base": gw_base,
        "gateway.session": gw_session,
    }.items():
        sys.modules.setdefault(name, mod)

    # --- Load the plugin package the way CI does (as ``carbonvoice``). ---
    pkg_name = "carbonvoice"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(ROOT)]
        sys.modules[pkg_name] = pkg
    for name in [
        "constants", "parse", "dedupe", "state", "api", "transport",
        "permits", "audit", "reactions", "channels", "gate", "conversations",
        "adapter",
    ]:
        mod_name = f"{pkg_name}.{name}"
        if mod_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(mod_name, ROOT / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

    return sys.modules["carbonvoice.adapter"], sys.modules["carbonvoice.constants"]


adapter_mod, constants_mod = _bootstrap()
api_mod = sys.modules["carbonvoice.api"]
state_mod = sys.modules["carbonvoice.state"]
CarbonVoiceAdapter = adapter_mod.CarbonVoiceAdapter
CarbonVoiceAPI = api_mod.CarbonVoiceAPI
InvalidCursorError = api_mod.InvalidCursorError
MessageUpdatesPage = api_mod.MessageUpdatesPage
Cursor = state_mod.Cursor

SEED = "2026-09-24T09:00:00+00:00"


# ── fetch_message_updates: request contract ─────────────────────────────


def _mock_api(handler):
    requests = []

    def _record(request):
        requests.append(request)
        return handler(request)

    api = CarbonVoiceAPI("cv_pat_test", "https://cv.test")
    api._client = httpx.AsyncClient(
        base_url="https://cv.test", transport=httpx.MockTransport(_record)
    )
    return api, requests


def _page_json(ids, has_more=False, next_cursor="CUR"):
    return {
        "data": [
            {"id": i, "conversation_id": "C1", "thread_id": i,
             "updated_at": "2026-09-24T10:00:00Z", "content": {"transcript": i}}
            for i in ids
        ],
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def test_first_page_is_date_anchored_and_later_pages_use_the_cursor():
    api, reqs = _mock_api(lambda r: httpx.Response(200, json=_page_json(["M1"])))

    page = asyncio.run(api.fetch_message_updates(date=SEED, conversation_id="C1"))
    first = dict(reqs[0].url.params)
    assert reqs[0].method == "GET" and reqs[0].url.path == "/v6/messages/updates"
    assert first == {"direction": "newer", "limit": "200", "date": SEED,
                     "conversation_id": "C1"}
    assert "channel_id" not in first  # rejected by the v6 deprecated-fields pipe
    assert page.has_more is False and page.next_cursor == "CUR"
    assert page.messages[0]["message_id"] == "M1"      # normalised to legacy
    assert page.messages[0]["channel_ids"] == ["C1"]

    asyncio.run(api.fetch_message_updates(cursor="CUR", date=SEED, limit=500))
    second = dict(reqs[1].url.params)
    assert second == {"direction": "newer", "limit": "200", "cursor": "CUR"}  # no date; clamped


def test_empty_date_anchored_page_has_null_cursor():
    api, _ = _mock_api(lambda r: httpx.Response(
        200, json={"data": [], "has_more": False, "next_cursor": None}))
    page = asyncio.run(api.fetch_message_updates(date=SEED))
    assert page.messages == [] and page.next_cursor is None


def test_400_on_a_cursor_raises_invalid_cursor_other_errors_raise_http():
    api, _ = _mock_api(lambda r: httpx.Response(400, json={"message": "Invalid cursor"}))
    with pytest.raises(InvalidCursorError):
        asyncio.run(api.fetch_message_updates(cursor="BAD"))

    api, _ = _mock_api(lambda r: httpx.Response(500, json={"message": "boom"}))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(api.fetch_message_updates(cursor="GOOD"))

    with pytest.raises(ValueError):
        asyncio.run(api.fetch_message_updates())


# ── adapter sync loop ───────────────────────────────────────────────────


def _msg(mid, updated="2026-09-24T10:00:00Z", created=None):
    return {
        "message_id": mid, "id": mid, "channel_ids": ["C1"],
        "last_updated_at": updated,
        "created_at": created or "2999-01-01T00:00:00Z",  # always "young"
    }


class FakeUpdatesAPI:
    """Scripted fetch_message_updates. ``script`` maps the request key —
    ("cursor", c) or ("date", d) — to a page or an exception."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    async def fetch_message_updates(self, *, cursor=None, date=None, **kw):
        key = ("cursor", cursor) if cursor else ("date", date)
        self.calls.append(key)
        result = self.script[key]
        if isinstance(result, Exception):
            raise result
        return result


def _page(msgs, has_more=False, next_cursor=None):
    return MessageUpdatesPage(messages=msgs, has_more=has_more, next_cursor=next_cursor)


def _make_adapter(api, tmp_path, *, cursor=None, seed=SEED, outcome=None):
    """Adapter with a fake API, a real Cursor and a recording processor.

    ``outcome`` maps message_id → return value (True default) or an
    exception to raise from _process_message.
    """
    a = object.__new__(CarbonVoiceAdapter)
    a._api = api
    a._cursor = Cursor(tmp_path / "state.json")
    a._cursor._cursor = cursor
    a._cursor._last_seen_at = seed
    a._polled = OrderedDict()
    a._tick_pending = False
    a._stuck_max_age_s = 300
    a.processed = []
    a.cursor_during_processing = []
    a.stuck_retries = 0
    outcome = outcome or {}

    async def _process(msg):
        mid = msg["message_id"]
        a.processed.append(mid)
        a.cursor_during_processing.append(a._cursor.cursor)
        result = outcome.get(mid, True)
        if isinstance(result, Exception):
            raise result
        return result

    async def _no_prompts(messages):
        return None

    def _stuck_retry():
        a.stuck_retries += 1

    a._process_message = _process
    a._check_pending_prompt_reactions = _no_prompts
    a._schedule_stuck_retry = _stuck_retry
    return a


def _sync(a):
    async def _run():
        await a._fetch_missed_messages_locked()
        await a._cursor.stop()  # flush to disk inside the loop
    asyncio.run(_run())


def test_first_run_seeds_the_date_and_fetches_nothing(tmp_path):
    api = FakeUpdatesAPI({})
    a = _make_adapter(api, tmp_path, seed=None)
    _sync(a)
    assert api.calls == []
    assert a._cursor.last_seen_at and a._cursor.cursor is None


def test_pages_follow_next_cursor_and_persist_the_tail_cursor(tmp_path):
    api = FakeUpdatesAPI({
        ("date", SEED): _page([_msg("M1"), _msg("M2")], has_more=True, next_cursor="C1"),
        ("cursor", "C1"): _page([_msg("M3")], has_more=True, next_cursor="C2"),
        ("cursor", "C2"): _page([_msg("M4")], has_more=False, next_cursor="TAIL"),
    })
    a = _make_adapter(api, tmp_path)
    _sync(a)

    assert api.calls == [("date", SEED), ("cursor", "C1"), ("cursor", "C2")]
    assert a.processed == ["M1", "M2", "M3", "M4"]
    # Persisted only after the whole batch was handled: while processing,
    # the stored cursor had not moved yet.
    assert a.cursor_during_processing == [None] * 4
    assert a._cursor.cursor == "TAIL"
    assert a._cursor.last_seen_at != SEED  # seed refreshed for re-anchoring
    on_disk = json.loads((tmp_path / "state.json").read_text())
    assert on_disk["cursor"] == "TAIL"
    assert a._tick_pending is False and a.stuck_retries == 0


def test_resume_uses_the_stored_cursor_and_never_the_date(tmp_path):
    api = FakeUpdatesAPI({("cursor", "STORED"): _page([], next_cursor="STORED")})
    a = _make_adapter(api, tmp_path, cursor="STORED")
    _sync(a)
    assert api.calls == [("cursor", "STORED")]
    assert a._cursor.cursor == "STORED"  # empty page via cursor echoes it


def test_empty_date_anchored_page_moves_only_the_seed(tmp_path):
    api = FakeUpdatesAPI({("date", SEED): _page([], next_cursor=None)})
    a = _make_adapter(api, tmp_path)
    _sync(a)
    assert a._cursor.cursor is None
    assert a._cursor.last_seen_at != SEED


def test_redelivered_rows_are_deduped_but_real_updates_pass(tmp_path):
    api = FakeUpdatesAPI({
        ("date", SEED): _page([_msg("M1"), _msg("M2")], next_cursor="C1"),
    })
    a = _make_adapter(api, tmp_path)
    _sync(a)
    assert a.processed == ["M1", "M2"]

    # Resume cursor re-delivers the ~4s window: M2 again (same
    # last_updated_at) plus a genuinely updated M1 and a new M3.
    api.script[("cursor", "C1")] = _page([
        _msg("M2"),
        _msg("M1", updated="2026-09-24T10:00:30Z"),
        _msg("M3"),
    ], next_cursor="C2")
    _sync(a)
    assert a.processed == ["M1", "M2", "M1", "M3"]
    assert a._cursor.cursor == "C2"


def test_invalid_stored_cursor_is_cleared_and_reanchored_by_date(tmp_path):
    api = FakeUpdatesAPI({
        ("cursor", "STALE"): InvalidCursorError("Invalid cursor"),
        ("date", SEED): _page([_msg("M1")], next_cursor="FRESH"),
    })
    a = _make_adapter(api, tmp_path, cursor="STALE")
    _sync(a)
    assert api.calls == [("cursor", "STALE"), ("date", SEED)]
    assert a.processed == ["M1"]
    assert a._cursor.cursor == "FRESH"


@pytest.mark.parametrize("error", [
    httpx.HTTPStatusError(
        "503", request=httpx.Request("GET", "https://cv.test"),
        response=httpx.Response(503)),
    httpx.HTTPStatusError(
        "401", request=httpx.Request("GET", "https://cv.test"),
        response=httpx.Response(401)),
    httpx.ConnectError("network down"),
])
def test_other_failures_keep_the_stored_cursor(tmp_path, error):
    api = FakeUpdatesAPI({("cursor", "KEEP"): error})
    a = _make_adapter(api, tmp_path, cursor="KEEP")
    _sync(a)
    assert a._cursor.cursor == "KEEP"
    assert a._cursor.last_seen_at == SEED
    assert a.processed == []


def test_failure_mid_sync_persists_only_handled_pages(tmp_path):
    api = FakeUpdatesAPI({
        ("cursor", "START"): _page([_msg("M1")], has_more=True, next_cursor="C1"),
        ("cursor", "C1"): httpx.ConnectError("network down"),
    })
    a = _make_adapter(api, tmp_path, cursor="START")
    _sync(a)
    assert a.processed == ["M1"]
    assert a._cursor.cursor == "C1"  # resume where the handled page ended
    assert a._cursor.last_seen_at == SEED  # sync incomplete: seed untouched


def test_stuck_message_holds_the_cursor_at_its_page(tmp_path):
    api = FakeUpdatesAPI({
        ("cursor", "START"): _page([_msg("M1")], has_more=True, next_cursor="C1"),
        ("cursor", "C1"): _page([_msg("M2"), _msg("M3")], has_more=True, next_cursor="C2"),
        ("cursor", "C2"): _page([_msg("M4")], next_cursor="TAIL"),
    })
    a = _make_adapter(api, tmp_path, cursor="START", outcome={"M2": None})
    _sync(a)
    assert a.processed == ["M1", "M2", "M3", "M4"]  # later messages still dispatch
    assert a._cursor.cursor == "C1"  # re-fetch the stuck page next sync
    assert a.stuck_retries == 1

    # Next sync re-delivers the held page: M3 (handled) is deduped, the
    # stuck M2 is retried.
    a.processed.clear()
    _sync(a)
    assert a.processed == ["M2"]


def test_processing_error_holds_the_cursor_like_a_stuck_message(tmp_path):
    api = FakeUpdatesAPI({
        ("cursor", "START"): _page([_msg("M1"), _msg("M2")], next_cursor="TAIL"),
    })
    a = _make_adapter(api, tmp_path, cursor="START",
                      outcome={"M2": RuntimeError("boom")})
    _sync(a)
    assert a._cursor.cursor == "START"  # not skipped past the failed message

    # An aged-out failure is let go so it can't pin the cursor forever.
    api.script[("cursor", "START")] = _page(
        [_msg("M2", created="2020-01-01T00:00:00Z")], next_cursor="TAIL")
    _sync(a)
    assert a._cursor.cursor == "TAIL"


def test_backlog_longer_than_one_sync_queues_a_trailing_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_mod, "UPDATES_MAX_PAGES_PER_TICK", 2)
    api = FakeUpdatesAPI({
        ("cursor", "START"): _page([_msg("M1")], has_more=True, next_cursor="C1"),
        ("cursor", "C1"): _page([_msg("M2")], has_more=True, next_cursor="C2"),
    })
    a = _make_adapter(api, tmp_path, cursor="START")
    _sync(a)
    assert len(api.calls) == 2
    assert a._cursor.cursor == "C2"
    assert a._tick_pending is True


# ── state persistence ───────────────────────────────────────────────────


def test_cursor_state_round_trips_and_reads_pre_v6_state(tmp_path):
    path = tmp_path / "state.json"

    async def _write():
        c = Cursor(path)
        c.set_cursor("CUR", seed_iso=SEED)
        await c.stop()
    asyncio.run(_write())
    assert json.loads(path.read_text()) == {"lastSeenAt": SEED, "cursor": "CUR"}

    async def _read():
        c = Cursor(path)
        await c.load()
        return c
    c = asyncio.run(_read())
    assert c.cursor == "CUR" and c.last_seen_at == SEED

    # State written before the migration has only lastSeenAt → date seed.
    path.write_text(json.dumps({"lastSeenAt": SEED}))
    c = asyncio.run(_read())
    assert c.cursor is None and c.last_seen_at == SEED

    async def _clear():
        c = Cursor(path)
        await c.load()
        c.clear_cursor()
        await c.stop()
    path.write_text(json.dumps({"lastSeenAt": SEED, "cursor": "CUR"}))
    asyncio.run(_clear())
    assert json.loads(path.read_text()) == {"lastSeenAt": SEED}
