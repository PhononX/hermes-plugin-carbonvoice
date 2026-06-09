# Upstreaming Carbon Voice as a native Hermes platform

How to promote this external plugin into a **native/built-in** platform in
Hermes Agent core. This is a planning doc — nothing here is done yet.

## TL;DR

- **Upstream repo:** https://github.com/NousResearch/hermes-agent (open source)
- **Procedure:** fork → branch from `main` → implement the 16-point checklist
  in core's [`gateway/platforms/ADDING_A_PLATFORM.md`](https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/ADDING_A_PLATFORM.md) → open a public PR.
- **`carbonvoice` is not referenced anywhere in core today** — we start clean.
- **~70% of this plugin ports as-is**; ~30% is new native integration glue.
- **Do this only after the plugin is stable and merged internally.** A public
  PR should start from mature, tested code. The plugin already has feature
  parity (cron, auth, streaming) without the 16-point maintenance burden.

## When to do it (vs. staying a plugin)

Convert to native only when CV should be a **first-class, bundled** platform —
shipped with every Hermes install, on the core release cycle. Until then the
plugin model is the right call: full feature parity, zero core-maintenance
surface.

## What ports as-is (~70%)

The adapter and its helper modules move almost verbatim into
`gateway/platforms/` (likely as a single `carbonvoice.py`, or a subpackage if
core allows it):

| This plugin | Native destination | Change |
|---|---|---|
| `adapter.py` (`CarbonVoiceAdapter`) | `gateway/platforms/carbonvoice.py` | imports only |
| `api.py`, `parse.py`, `transport.py`, `state.py`, `dedupe.py`, `reactions.py`, `channels.py`, `audit.py`, `permits.py`, `gate.py`, `conversations.py`, `constants.py` | same package | imports only |
| `Platform("carbonvoice")` dynamic lookup | `Platform.CARBONVOICE` enum member | one-line |

The business logic — deny-by-default gate, PairingStore reuse, reactions,
dedup, cursor handling, 502 retry — is platform-agnostic and unchanged.

## What's new native glue (~30%) — the 16-point checklist

Per core's `ADDING_A_PLATFORM.md`. Each is small; the work is breadth, not depth.

1. **NEW** `gateway/platforms/carbonvoice.py` — the adapter (port of `adapter.py`)
2. `gateway/config.py` — add `CARBONVOICE = "carbonvoice"` to the `Platform` enum
3. `gateway/config.py` — env-var block in `_apply_env_overrides()` (read `CARBONVOICE_PAT` etc.)
4. `gateway/run.py` — `_create_adapter()`: `elif platform == Platform.CARBONVOICE: …`
5. `gateway/run.py` — auth maps: `CARBONVOICE_ALLOWED_USERS` → `platform_env_map`, `CARBONVOICE_ALLOW_ALL_USERS` → `platform_allow_all_map`
6. `gateway/session.py` — custom `SessionSource` fields (only if needed; likely not)
7. `agent/prompt_builder.py` — add a `"carbonvoice"` entry to `PLATFORM_HINTS`
8. `tools/send_message_tool.py` — `_send_to_platform()` case + `_send_carbonvoice(...)` standalone sender
9. `cron/scheduler.py` — add to `_KNOWN_DELIVERY_PLATFORMS` + `_HOME_TARGET_ENV_VARS` (`CARBONVOICE_HOME_CHANNEL`)
10. `toolsets.py` — `"hermes-carbonvoice"` toolset + include in `"hermes-gateway"`
11. `hermes_cli/gateway.py` — setup-wizard metadata (token var, instructions, vars)
12. `hermes_cli/status.py` — status display entry
13. `tools/cronjob_tools.py` — mention carbonvoice in the `deliver` param description
14. `agent/redact.py` — redaction patterns (only if CV ids are PII-sensitive)
15. `gateway/channel_directory.py` — session-based discovery (if needed)
16. `tests/gateway/test_carbonvoice.py` — port our CI smoke asserts into core's test suite

## Mapping our env vars → native config

All `CARBONVOICE_*` vars already work as env vars (the dominant native path).
The plugin-only YAML translation hook (`apply_yaml_config_fn`) is dropped; if
core wants `config.yaml` keys, fold them into `load_gateway_config()`.

Vars to carry over (see `plugin.yaml` for the full list + descriptions):
`CARBONVOICE_PAT` (required), `_BASE_URL`, `_ALLOWED_USERS`, `_ALLOW_ALL_USERS`,
`_HOME_CHANNEL`, `_APPROVAL_COOLDOWN_S`, `_PENDING_REACTION_ID`, `_REACTION_ID`,
`_STUCK_MAX_AGE_S`, `_SEND_DEDUP_WINDOW_S`, `_POLL_INTERVAL_MS`, `_WS_RETRY_MAX_MS`,
`_REQUIRE_MENTION`, `_MAX_ATTACHMENT_MB`, `_VOICE_OUT`, and the rest.

## Suggested sequence

1. **Stabilize + merge PR #19 internally.** (in progress)
2. Soak in production; confirm the duplication/cursor/latency fixes hold.
3. Fork `NousResearch/hermes-agent`, branch `feat/carbon-voice-native-platform` off `main`.
4. Port the adapter + helpers into `gateway/platforms/`.
5. Walk the 16-point checklist; check each against `ADDING_A_PLATFORM.md`.
6. Port CI smoke asserts into `tests/gateway/test_carbonvoice.py`.
7. Open the public PR referencing the checklist (show every box ticked).

## Open questions for the core team

- Single file vs. subpackage under `gateway/platforms/`? (we have ~12 helper modules)
- Are the new CV deps acceptable in core's `pyproject.toml` (httpx, python-socketio)?
- Do they want `config.yaml` keys, or env-vars-only?
