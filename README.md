# hermes-plugin — Carbon Voice

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin by [PhononX](https://github.com/PhononX) that connects Hermes to [Carbon Voice](https://carbonvoice.app), so the Hermes agent appears as a bot user inside Carbon Voice channels.

## ⚡ Quickstart (60 seconds)

You need Hermes already installed and a Carbon Voice Personal Access Token (grab one at <https://www.developer.carbonvoice.app/>).

```bash
# 1. Install the plugin
hermes plugins install PhononX/hermes-plugin-carbonvoice --enable

# 2. (Recommended) install the optional Socket.IO client for real-time delivery
~/.hermes/hermes-agent/venv/bin/pip install 'python-socketio[asyncio_client]'

# 3. Add your PAT + open the bot to all senders (dev) — append to ~/.hermes/.env
cat >> ~/.hermes/.env <<'EOF'
CARBONVOICE_PAT=cv_pat_xxxxxxxxxxxxxxxxxxxxxxxx
CARBONVOICE_ALLOW_ALL_USERS=true
EOF

# 4. Run Hermes
hermes gateway run
```

Now DM the agent from any Carbon Voice account — it reacts with ✅ within a second and replies in-thread. To restrict who can talk to it, replace `CARBONVOICE_ALLOW_ALL_USERS=true` with `CARBONVOICE_ALLOWED_USERS=<your_user_guid>` (your guid prints on startup as `connected as <guid>`).

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

Add one line to `~/.hermes/.env`:

```bash
CARBONVOICE_PAT=cv_pat_...
```

By default the bot rejects all senders. For dev, the easiest unlock is:

```bash
CARBONVOICE_ALLOW_ALL_USERS=true
```

To restrict who can trigger the bot, replace `CARBONVOICE_ALLOW_ALL_USERS` with `CARBONVOICE_ALLOWED_USERS=<your_carbonvoice_user_guid>`. Your own `user_guid` shows up in the gateway logs as `carbonvoice: connected as <guid>` on startup.

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
| `CARBONVOICE_ALLOWED_USERS` | _(unset)_ | Comma-separated `user_guid`s allowed to trigger the bot. |
| `CARBONVOICE_ALLOW_ALL_USERS` | `false` | Allow any Carbon Voice user (dev only). |
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
