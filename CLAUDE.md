# Carbon Voice plugin for Hermes

Python 3.10+ async plugin that connects the Hermes Agent gateway to Carbon Voice (`https://api.carbonvoice.app`). Flat package: all modules live at the repo root. The directory name is hyphenated (`hermes-pluging`), so it can't be imported by its own name — the gateway loads it via `setup.py:register`.

## Architecture

- [adapter.py](adapter.py) — the heart: `CarbonVoiceAdapter(BasePlatformAdapter)`. Lifecycle (`connect()` → whoami → transport), message normalization into `MessageEvent`, send paths, admin commands (`/cv-allow-user`, …), deny-by-default allow-list seeded from `whoami.created_by` (the bot's creator is the owner).
- [api.py](api.py) — `CarbonVoiceAPI`, the only REST client (httpx). All endpoints live here. `standalone_send()` is a one-shot client for out-of-process cron delivery.
- [transport.py](transport.py) — connection strategy: Socket.IO primary (`message:created`/`message:updated`), REST polling fallback with WS reconnect backoff. Calls `on_tick`; never touches payloads.
- [parse.py](parse.py) — pure payload/parsing helpers, no I/O. `client_headers()` builds the default header set.
- [constants.py](constants.py) — all tunables and defaults, each documented inline.
- Supporting: [gate.py](gate.py) (authorization gate), [permits.py](permits.py) + [reactions.py](reactions.py) (reaction-based owner approval), [channels.py](channels.py) / [conversations.py](conversations.py) (channel context), [dedupe.py](dedupe.py) + [state.py](state.py) (seen-cache, cursor persistence), [audit.py](audit.py) (audit log).

## Conventions and invariants

- **Custom headers are lowercase-hyphenated, no `x-` prefix** (Carbon Voice-wide convention). Every request carries `agent-name: hermes`; after `/whoami` resolves, also `agent-id: <user_guid>` (see `set_agent_id`).
- **The User-Agent is what the backend actually logs** — cv-api's request logger only captures `ua`/`mobile-app-version`/`platform`, not arbitrary headers. So every request sends `user-agent: hermes-plugin/<version>` (version read from plugin.yaml), upgraded to `hermes-plugin/<version> (agent-id: <guid>)` after whoami. Keep this in sync if the backend ever starts logging `agent-name`/`agent-id` directly.
- **Auth:** PAT starting with `cv_pat_` → `Authorization: Bearer`; anything else → `x-api-key`.
- **Sends are NEVER retried** — a retried send can duplicate a delivered message. Only idempotent reads/reactions retry on transient 502/503/504 (`_request_retrying`).
- **Do not add headers to S3 presigned PUT/GET requests** — extra headers can break the signature. Those calls intentionally use a plain client.
- **Voice-out (auto-TTS) is disabled by CTO decision:** Carbon Voice does STT+TTS itself; Hermes replies in text only.
- Inbound `media_urls` must be bare local paths, not `file://` URIs.
- Hold the poll cursor on stuck (not-yet-transcribed) messages rather than skipping them; give up after `DEFAULT_STUCK_MAX_AGE_S`.

## Environment

`CARBONVOICE_PAT` (required), `CARBONVOICE_BASE_URL` (default `https://api.carbonvoice.app`), `CARBONVOICE_ALLOWED_USERS`, `CARBONVOICE_REACTION_ID` and friends (see constants.py).

## Dev workflow

- The plugin runs inside a Hermes gateway checkout (`~/Documents/Development/hermes-agent` for upstream-PR work, `hermes-production` for the cloud deploy). Dev install is a symlink into the gateway; production installs clone from GitHub.
- Dependencies: `httpx>=0.27` required; `python-socketio[asyncio_client]>=5` optional — without it the transport falls back to REST polling (the gateway never auto-installs plugin deps).
- Quick import sanity check without the gateway: symlink the repo to an underscored name in /tmp, stub the `gateway.*` modules, and import `adapter`/`api`/`transport` (the gateway types are only needed at class-definition time).
- See [DEVELOPMENT.md](DEVELOPMENT.md) for the full setup and [UPSTREAMING.md](UPSTREAMING.md) for the plan to land Carbon Voice as a native Hermes platform.
