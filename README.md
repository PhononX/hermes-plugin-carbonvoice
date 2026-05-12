# hermes-plugin — Carbon Voice

[![ci](https://github.com/PhononX/hermes-plugin-carbonvoice/actions/workflows/ci.yml/badge.svg)](https://github.com/PhononX/hermes-plugin-carbonvoice/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin by [PhononX](https://github.com/PhononX) that connects Hermes to [Carbon Voice](https://carbonvoice.app), so the Hermes agent appears as a bot user inside Carbon Voice channels.

## ⚡ Quickstart (60 seconds)

You need Hermes already installed and a Carbon Voice Personal Access Token (grab one at <https://www.developer.carbonvoice.app/>).

### 1. Install the plugin

The installer prompts you for your Carbon Voice PAT — paste it in and press Enter.

```bash
hermes plugins install PhononX/hermes-plugin-carbonvoice --enable
```

### 2. Start Hermes

```bash
hermes gateway run
```

On the first run, the plugin auto-installs `python-socketio` (for real-time delivery via Socket.IO) if it's not already present — you'll see one log line and then the gateway starts normally. If the install fails (offline, sandboxed venv, etc.) the plugin still works in REST-polling mode.

On startup you'll see `carbonvoice: connected as <your_user_guid>` — handy if you decide to restrict access later.

### 3. Send a message from Carbon Voice

Open Carbon Voice (web, mobile, or desktop) and DM the agent's account. It reacts with ✅ within a second and replies in-thread.

---

### Optional: restrict who can talk to the bot

By default the bot accepts messages from any Carbon Voice user (good for personal/dev setups). To limit access to specific people, add their `user_guid`s to `~/.hermes/.env`:

```bash
echo 'CARBONVOICE_ALLOWED_USERS=<your_user_guid>,<teammate_guid>' >> "$(hermes config env-path)"
```

> 💡 Prefer a GUI for editing the `.env`? Run `open $(hermes config env-path)` to open it in your default editor, or `hermes dashboard` for the web UI at <http://127.0.0.1:9119>.

---

## What it does

- **No webhook, no tunnel.** Connects via Socket.IO (primary) and polls `POST /v3/messages/recent` as a REST fallback.
- **Offline catch-up.** Persists a cursor to `$HERMES_HOME/state/carbonvoice.json`, so messages that arrived while Hermes was down are processed on the next startup.
- **Visual ack on receipt.** Reacts to every inbound message with a Carbon Voice reaction (default: `acknowledged`) so users see feedback in <100ms even before the agent finishes thinking.
- **Mark-as-read.** Clears the unread notification once the agent has handled the message.
- **Usernames in logs.** Resolves `user_guid` → display name via `GET /v3/users/{id}` and caches in memory.
- **Audit log of rejected senders.** Any message dropped by the allowlist is appended to `$HERMES_HOME/logs/carbonvoice-ignored-senders.log` with timestamp + resolved username.
- **Self-loop filtered** out via the agent's own `user_guid`.
- **Text-only.** Carbon Voice transcribes voice messages to text before delivery; transcripts arrive in two phases (`message:created` → `message:updated`) and the adapter waits for the populated transcript before dispatching.

## Requirements

- Hermes Agent installed and configured (`hermes setup` already done with an LLM provider).
- A [Carbon Voice](https://carbonvoice.app) account for the identity the agent will use.
- A Carbon Voice Personal Access Token — get one at <https://www.developer.carbonvoice.app/>.
- `httpx` (already in the Hermes venv). Optional: `python-socketio[asyncio_client]` for real-time WebSocket delivery — without it the adapter runs in polling-only mode and still works.

## Configure

Add one line to `~/.hermes/.env` (the install wizard does this for you on `--enable`):

```bash
CARBONVOICE_PAT=cv_pat_...
```

That's the only required variable. By default the bot accepts messages from any Carbon Voice user. To restrict, set:

```bash
CARBONVOICE_ALLOWED_USERS=<your_carbonvoice_user_guid>[,<another_guid>...]
```

To explicitly close the bot to everyone (e.g., temporary maintenance mode), set `CARBONVOICE_ALLOW_ALL_USERS=false` and leave `CARBONVOICE_ALLOWED_USERS` empty. Your own `user_guid` shows up in the gateway logs as `carbonvoice: connected as <guid>` on startup.

## Run

```bash
hermes gateway run
```

You should see:

```
carbonvoice: connected as <your_user_guid> (mode=websocket, state=…/carbonvoice.json)
carbonvoice: Socket.IO connected
```

If `python-socketio` is not installed, you'll see `mode=polling` instead — equally functional, just polls every 5 seconds.

Now DM the agent's Carbon Voice account from another account. Hermes replies in the same channel, threaded to your message.

If Hermes is restarted, any messages that arrived while it was offline are fetched via `/v3/messages/recent` on startup and processed before the live connection comes up.

## Optional environment variables

| Variable | Default | Description |
|---|---|---|
| `CARBONVOICE_BASE_URL` | `https://api.carbonvoice.app` | Carbon Voice API base URL. |
| `CARBONVOICE_POLL_INTERVAL_MS` | `5000` | Polling interval (when WS is down or unavailable). |
| `CARBONVOICE_WS_RETRY_MAX_MS` | `30000` | Max WebSocket reconnect backoff. |
| `CARBONVOICE_STATE_PATH` | `$HERMES_HOME/state/carbonvoice.json` | Path to the cursor state file. |
| `CARBONVOICE_CREATOR_ID` | _(unset)_ | Restrict inbound messages to a single Carbon Voice `user_guid`. |
| `CARBONVOICE_ALLOWED_USERS` | _(unset)_ | Comma-separated `user_guid`s allowed to trigger the bot. When set, **only** these users are accepted. |
| `CARBONVOICE_ALLOW_ALL_USERS` | `true` | Default open access. Set to `false` (and leave `CARBONVOICE_ALLOWED_USERS` empty) to explicitly close the bot. |
| `CARBONVOICE_HOME_CHANNEL` | _(unset)_ | Default `channel_guid` for cron/notification delivery. |
| `CARBONVOICE_HOME_CHANNEL_NAME` | _(unset)_ | Display name for the home channel. |
| `CARBONVOICE_REACTION_ID` | `acknowledged` | Reaction id used to ack inbound messages. Available ids are logged on startup; pin a different one with this var. |
| `CARBONVOICE_DISABLE_ACK_REACTION` | `false` | Disable the visual ack reaction. |
| `CARBONVOICE_DISABLE_MARK_READ` | `false` | Disable clearing the unread notification after the agent replies. |
| `CARBONVOICE_IGNORED_SENDERS_LOG` | `$HERMES_HOME/logs/carbonvoice-ignored-senders.log` | Path to the audit log of rejected senders (one JSON line per rejection). |

## Architecture

```
┌──────────────────────────────┐
│  Hermes gateway              │
│                              │
│   CarbonVoiceAdapter         │
│   ├── Socket.IO client ───────────▶  api.carbonvoice.app
│   │   (message:created /              (real-time push)
│   │    message:updated → REST fetch)
│   │
│   └── REST polling fallback ─────▶  POST /v3/messages/recent
│       (every 5s while WS is down)    { date: lastSeenAt, direction: "newer" }
│
│   Outbound ─────────────────────▶  POST /v3/messages/start
│
│   State cursor (debounced 5s flush)
│   $HERMES_HOME/state/carbonvoice.json
└──────────────────────────────┘
```

The adapter never accepts inbound HTTP — both transports are outbound-initiated, so it works behind NAT without a tunnel.

## Troubleshooting

**`401 Unauthorized` on `/whoami`** — your PAT is wrong, expired, or revoked. Generate a new one at https://www.developer.carbonvoice.app/.

**`No user allowlists configured` warning** — the bot rejects all senders until you set `CARBONVOICE_ALLOW_ALL_USERS=true` or `CARBONVOICE_ALLOWED_USERS=<guid>`.

**Messages from voice notes don't arrive** — transcription can take a few seconds. The adapter waits for `message:updated` (or the next poll) to pick up the populated transcript. If a transcript never arrives, check the Carbon Voice account has transcription enabled.

**State file getting out of sync** — delete `$HERMES_HOME/state/carbonvoice.json` to reset the cursor (the next start will pick up from "now").

## License

MIT
