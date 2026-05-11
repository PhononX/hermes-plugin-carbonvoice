# hermes-plugin — Carbon Voice

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin by [PhononX](https://github.com/PhononX) that connects Hermes to [Carbon Voice](https://carbonvoice.app), so the Hermes agent appears as a bot user inside Carbon Voice channels.

- Receives inbound messages via Carbon Voice webhooks (`message.posted.to.channel`).
- Replies via `POST /v3/messages/start`, threaded to the user's incoming message.
- Self-loop filtered out via Carbon Voice's `creator_id ne <self>` subscription filter.
- Text-only — Carbon Voice transcribes voice messages to text before delivery.

## Requirements

- Hermes Agent installed and configured (`hermes setup` already done with an LLM provider).
- A [Carbon Voice](https://carbonvoice.app) account for the identity the agent will use.
- A Carbon Voice Personal Access Token — get one at https://www.developer.carbonvoice.app/.
- A public HTTPS URL that forwards to your machine. The simplest path is [ngrok](https://ngrok.com/) (free tier works for dev). For sustained use, prefer ngrok with a static domain or a Cloudflare Tunnel.

## Install

```bash
hermes plugins install PhononX/hermes-plugin-carbonvoice --enable
```

That drops the plugin into `~/.hermes/plugins/carbonvoice/` and enables it.

## Configure

Add three lines to `~/.hermes/.env`:

```bash
CARBONVOICE_PAT=cv_pat_...
CARBONVOICE_PUBLIC_WEBHOOK_BASE_URL=https://xxxx.ngrok-free.dev
CARBONVOICE_ALLOW_ALL_USERS=true
```

`CARBONVOICE_ALLOW_ALL_USERS=true` is the easy default for dev. To restrict who can trigger the bot, replace it with `CARBONVOICE_ALLOWED_USERS=<your_carbonvoice_user_guid>` (your `user_guid` shows up in the gateway logs as `carbonvoice: connected as <guid>` on startup).

## Run

In one terminal, start ngrok pointing at the plugin's default port:

```bash
ngrok http 8645
```

Copy the `https://...ngrok-free.dev` URL it prints and make sure it matches `CARBONVOICE_PUBLIC_WEBHOOK_BASE_URL` in `.env` (the ngrok free tier rotates the URL each restart).

In another terminal, start Hermes:

```bash
hermes gateway run
```

You should see:

```
carbonvoice: connected as <your_user_guid>
carbonvoice: subscribed webhook https://xxxx.ngrok-free.dev/carbonvoice/webhook
```

Now DM the agent's Carbon Voice account from another account. Hermes replies in the same channel, threaded to your message.

## Optional environment variables

| Variable | Default | Description |
|---|---|---|
| `CARBONVOICE_BASE_URL` | `https://api.carbonvoice.app` | Carbon Voice API base URL. |
| `CARBONVOICE_WEBHOOK_PATH` | `/carbonvoice/webhook` | Path under the public base URL where Carbon Voice POSTs. |
| `CARBONVOICE_PORT` | `8645` | Local port the adapter listens on. |
| `CARBONVOICE_BIND_HOST` | `0.0.0.0` | Local bind address. |
| `CARBONVOICE_CREATOR_ID` | _(unset)_ | Restrict inbound messages to a single Carbon Voice `user_guid`. |
| `CARBONVOICE_ALLOWED_USERS` | _(unset)_ | Comma-separated `user_guid`s allowed to trigger the bot. |
| `CARBONVOICE_ALLOW_ALL_USERS` | `false` | Allow any Carbon Voice user (dev only). |
| `CARBONVOICE_WEBHOOK_AUTH_HEADER_NAME` | _(unset)_ | Header name for shared-secret verification on inbound webhooks. |
| `CARBONVOICE_WEBHOOK_AUTH_HEADER_VALUE` | _(unset)_ | Expected value for the auth header (constant-time compared). |
| `CARBONVOICE_HOME_CHANNEL` | _(unset)_ | Default `channel_guid` for cron/notification delivery. |
| `CARBONVOICE_HOME_CHANNEL_NAME` | _(unset)_ | Display name for the home channel. |

## Troubleshooting

**`401 Unauthorized` on `/whoami`** — your PAT is wrong, expired, or revoked. Generate a new one at https://www.developer.carbonvoice.app/.

**`EADDRINUSE: port 8645`** — another process is on the default port. Change `CARBONVOICE_PORT` and restart `ngrok http <new_port>` accordingly.

**Webhook doesn't fire when you send a message** — check the ngrok web UI at http://127.0.0.1:4040. If no request hits it, the Carbon Voice subscription points at a stale URL. Restart the gateway after updating `CARBONVOICE_PUBLIC_WEBHOOK_BASE_URL`.

**`No user allowlists configured` warning** — the bot rejects all senders until you set `CARBONVOICE_ALLOW_ALL_USERS=true` or `CARBONVOICE_ALLOWED_USERS=<guid>`.

## License

MIT
