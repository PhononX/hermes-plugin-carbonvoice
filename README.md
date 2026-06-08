# hermes-plugin — Carbon Voice

[![ci](https://github.com/PhononX/hermes-plugin-carbonvoice/actions/workflows/ci.yml/badge.svg)](https://github.com/PhononX/hermes-plugin-carbonvoice/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin by [PhononX](https://github.com/PhononX) that connects Hermes to [Carbon Voice](https://carbonvoice.app), so the Hermes agent appears as a bot user inside Carbon Voice channels.

## ⚡ Quickstart (60 seconds)

You need Hermes already installed and a Carbon Voice Personal Access Token (grab one at <https://www.developer.carbonvoice.app/>).

### 1. Install the WebSocket client

`hermes plugins install` clones the plugin but does **not** run `pip install` on its dependencies (security boundary — see [Hermes plugin guide](https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin)). Install `python-socketio` manually so the plugin can deliver messages in real time:

```bash
python -m pip install 'python-socketio[asyncio_client]>=5'
```

> If you skip this step, the plugin still works — it falls back to REST polling (5s latency). You'll see a clear warning at gateway startup with the install command.

### 2. Install the plugin

The installer prompts you for your Carbon Voice PAT — paste it in and press Enter.

```bash
hermes plugins install PhononX/hermes-plugin-carbonvoice --enable
```

### 3. Start Hermes

```bash
hermes gateway run
```

On startup you'll see `carbonvoice: connected as <your_user_guid>` — handy if you decide to restrict access later.

### 4. Send a message from Carbon Voice

Open Carbon Voice (web, mobile, or desktop) and DM the agent's account. It reacts with ✅ within a second and replies in-thread.

---

### Who can talk to the bot (deny-by-default)

**The bot only responds to authorized users.** Since the agent can read and run things on the host, an open bot is a security hole — so access is **deny-by-default**. A user is allowed if any of these holds:

- **They're the owner** — the Carbon Voice user who *created* the bot account (`whoami.created_by`). Auto-detected at startup; **always allowed, no setup needed.**
- They're listed in `CARBONVOICE_ALLOWED_USERS` (a static comma-separated list).
- The owner approved them at runtime (see below).

**Interactive onboarding.** When an unauthorized user messages the bot, it asks **you** (the owner) in the home channel:

> 👤 *Teammate Name (Abc123…) wants to talk to me but isn't authorized.*
> *To allow them, reply:* `/cv-allow Abc123…`
> *To block them, reply:* `/cv-deny Abc123…`

Reply `/cv-allow <user_guid>` and they're approved instantly (persisted, survives restarts — no `.env` edit, no restart). Also: `/cv-deny <user_guid>`, `/cv-list`. (Set `CARBONVOICE_HOME_CHANNEL` so the bot knows where to ask you.)

To add people up front instead, set `CARBONVOICE_ALLOWED_USERS`:

```bash
echo 'CARBONVOICE_ALLOWED_USERS=<your_user_guid>,<teammate_guid>' >> "$(hermes config env-path)"
```

To go back to the old open behavior, set `CARBONVOICE_ALLOW_ALL_USERS=true`.

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
- `httpx` (already in the Hermes venv).
- `python-socketio[asyncio_client]` — **install manually** with `python -m pip install 'python-socketio[asyncio_client]>=5'`. Hermes does not auto-install plugin dependencies (security boundary). Without it, the adapter runs in REST polling mode and you'll see a warning at startup.

## Configure

Add one line to `~/.hermes/.env` (the install wizard does this for you on `--enable`):

```bash
CARBONVOICE_PAT=cv_pat_...
```

That's the only required variable. By default the bot accepts messages from any Carbon Voice user. To restrict, set:

```bash
CARBONVOICE_ALLOWED_USERS=<your_carbonvoice_user_guid>[,<another_guid>...]
```

Access is **deny-by-default**: only the owner (auto-detected), users in `CARBONVOICE_ALLOWED_USERS`, and users approved via `/cv-allow` can talk to the bot. To disable gating entirely (open to everyone), set `CARBONVOICE_ALLOW_ALL_USERS=true`. Your own `user_guid` shows up in the gateway logs as `carbonvoice: owner is <guid>` on startup.

## Run

```bash
hermes gateway run
```

You should see:

```
carbonvoice: connected as <your_user_guid> (mode=websocket, state=…/carbonvoice.json)
carbonvoice: Socket.IO connected
```

If `python-socketio` is not installed, you'll see a warning like:

```
carbonvoice: Carbon Voice realtime websocket support is unavailable because python-socketio is not installed. Falling back to REST polling. To enable websocket mode, install python-socketio[asyncio_client] in the Hermes venv: python -m pip install 'python-socketio[asyncio_client]>=5'
```

The adapter then runs in polling-only mode (functional, 5s latency).

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
| `CARBONVOICE_ALLOWED_USERS` | _(unset)_ | Comma-separated `user_guid`s allowed, *in addition to* the auto-detected owner and `/cv-allow`-approved users. |
| `CARBONVOICE_ALLOW_ALL_USERS` | `false` | **Deny-by-default.** Set to `true` to disable gating and let anyone talk to the bot (the old open behavior). |
| `CARBONVOICE_HOME_CHANNEL` | _(unset)_ | Default `channel_guid` for cron/notification delivery. |
| `CARBONVOICE_HOME_CHANNEL_NAME` | _(unset)_ | Display name for the home channel. |
| `CARBONVOICE_REACTION_ID` | `acknowledged` | Reaction id used to ack inbound messages. Available ids are logged on startup; pin a different one with this var. |
| `CARBONVOICE_DISABLE_ACK_REACTION` | `false` | Disable the visual ack reaction. |
| `CARBONVOICE_DISABLE_MARK_READ` | `false` | Disable clearing the unread notification after the agent replies. |
| `CARBONVOICE_IGNORED_SENDERS_LOG` | `$HERMES_HOME/logs/carbonvoice-ignored-senders.log` | Path to the audit log of rejected senders (one JSON line per rejection). |
| `CARBONVOICE_REQUIRE_MENTION` | `true` | In group channels, only respond when the agent is `@`-mentioned. DMs are always processed. Set to `false` to make the bot respond to every message in every channel it can see (the pre-gate behavior — useful for personal-bot setups). |
| `CARBONVOICE_FREE_RESPONSE_CHANNELS` | _(unset)_ | Comma-separated `channel_guid`s where the agent always responds, regardless of mention. Useful for channels dedicated to the bot. |
| `CARBONVOICE_IGNORED_CHANNELS` | _(unset)_ | Comma-separated `channel_guid`s where the agent **never** responds, even when mentioned. Hard veto — also applies to DMs if the channel guid is listed. |
| `CARBONVOICE_SHARED_GROUP_SESSIONS` | `false` | When `true`, all participants in a group channel share one session with the agent even outside of threads. Default `false` — sessions are already shared per thread (Hermes automatically adds `[sender name]` prefixes to each user message so the agent can attribute statements). Set `true` for bot-room channels where strict per-user isolation isn't wanted. |

### Mention behavior

The agent uses a DM-vs-channel split that mirrors how Slack/Discord/Telegram adapters in Hermes core gate messages:

- **DMs (1:1):** always processed.
- **Group channels:** require an `@`-mention of the agent unless the channel is in `CARBONVOICE_FREE_RESPONSE_CHANNELS` or `CARBONVOICE_REQUIRE_MENTION=false`.
- **Ignored channels:** never processed, even when mentioned.

Mention detection is **structured**: the adapter checks the message's `tagged_user_ids` array for the agent's `user_guid`. Carbon Voice's Flutter client sends the tag list directly (text messages carry it on send; voice memos add it via the tagging UI after recording), and the transcript itself is plain text — so the adapter never parses names out of the message body.

> ⚠️ **Voice-only messages:** mentions made by *speaking* the agent's name without using the tagging UI will not be detected, because nothing populates `tagged_user_ids`. To mention the agent from a voice memo, tag it in the picker (the tag is applied after the recording is transcribed, and the agent picks it up automatically).

### Voice replies (auto-TTS) — optional, off by default

> ℹ️ **Disabled by default.** The plugin ships with `CARBONVOICE_VOICE_OUT` off and Hermes core's `voice.auto_tts` defaults to `false`, so out of the box the agent replies in **text** and Carbon Voice handles speech (STT on the way in, and any read-aloud on its own side). This section is opt-in — enable it only if you specifically want Hermes itself to synthesize voice-memo replies.

Carbon Voice is voice-first, so the plugin *can* run the agent's text replies through Hermes' TTS pipeline and ship them as voice memos. The recipient sees a single bubble — a play button with the transcript inline (Carbon Voice transcribes the audio server-side) — threaded as a reply to the original message, mirroring how a human responds on the platform.

#### Setup checklist (three pieces, all required)

| # | Where | Setting | Purpose |
|---|---|---|---|
| 1 | `~/.hermes/.env` | `CARBONVOICE_VOICE_OUT=true` | The plugin marks every inbound as `MessageType.VOICE` so Hermes core's auto-TTS gate accepts the event. |
| 2 | `~/.hermes/config.yaml` | `voice.auto_tts: true` | Opts the gateway into the TTS pipeline globally. Hermes core default is `false`. |
| 3 | `~/.hermes/config.yaml` | `tts.provider: <name>` | TTS engine that synthesizes the audio. `edge` (Microsoft Edge TTS) is the default — free, no API key. Alternatives: `elevenlabs`, `openai`, `xai`, `mistral`, `neutts` — each needs its own API key in `.env`. |

Then restart: `hermes gateway restart`.

#### Worked example: Spanish voice with the free `edge` provider

`~/.hermes/.env` (append):
```bash
CARBONVOICE_VOICE_OUT=true
```

`~/.hermes/config.yaml` (edit the existing `voice:` and `tts:` blocks):
```yaml
voice:
  auto_tts: true              # was: false
  # …rest of voice config…

tts:
  provider: edge              # already the default
  edge:
    voice: es-MX-DaliaNeural  # mexican female; pick any edge voice
  # …other providers…
```

Edge voices in Spanish worth trying: `es-MX-DaliaNeural` (MX female), `es-MX-JorgeNeural` (MX male), `es-AR-ElenaNeural` (AR female), `es-AR-TomasNeural` (AR male), `es-ES-AlvaroNeural` (Castilian male). English default is `en-US-AriaNeural`.

#### What the user sees

With the three pieces in place, every text reply from the agent is auto-converted to audio and shipped through `POST /v5/messages/audio`. The recipient sees one bubble (voice memo + CV's server-side transcript, threaded under their original message). Behind the scenes:

```
agent text reply
    ↓
Hermes core auto-TTS  →  /tmp/tts_xxx.mp3 (via edge / elevenlabs / …)
    ↓
adapter.send_voice    →  POST /v5/messages/audio (multipart)
    ↓
CV backend runs STT   →  voice memo bubble with transcript inline
```

The bundled `platform_hint` reminds the agent that its replies will be spoken when voice-out is active — short sentences, no markdown tables, spelled-out symbols, ~30s ceiling. Long structured artifacts (code, JSON, tables) should be attached as files via `MEDIA:<path>` so the voice memo stays short and the artifact remains downloadable.

#### Turning it off

Any of the three flags reverts the behavior. The fastest is unsetting the env var:

```bash
# in ~/.hermes/.env
CARBONVOICE_VOICE_OUT=false
```

Then `hermes gateway restart`. Existing deployments that never opt in stay text-only.

### Multimodal input (images)

Carbon Voice users can attach images to a message via the standard CV UI — photos, screenshots, logos, diagrams. The plugin downloads each inbound image to `~/.hermes/image_cache/` and hands it to the agent via Hermes core's multimodal pipeline, so Claude vision (or any vision-capable model) sees the bytes natively and can describe / analyze the picture.

| Attachment type | Status |
|---|---|
| `image/*` (jpg, png, webp, gif, …) | ✅ Supported — downloaded + passed to vision |
| `type:"link"` (CV's share-link UI) | ✅ Supported — URL prepended to the message text so the agent fetches it with its own browser / web tools |
| Everything else (PDFs, text files, audio, binaries) | ⚠️ Logged at WARNING and skipped — the text part of the message still reaches the agent |

**Why not PDFs / text files yet?** Hermes core doesn't ship a native document-extraction pipeline. If the plugin handed the agent a `file://...pdf` path, the agent would fall through to `read_file` (returns binary garbage), then try to invoke `terminal` / `execute_code` to run `pdftotext` or `pypdf` — which triggers a permission prompt on the operator side instead of returning a useful answer. Skipping PDFs cleanly is a better default than producing that confusing UX. Document support is queued for a follow-up PR that extracts text in the adapter and prepends it to the agent's message context.

**Voice memos** remain transcript-only. CV transcribes the audio server-side before delivery, so the agent reads the transcribed text via the normal message body; the `audio_models[]` raw audio file isn't pulled into the agent context.

**Authentication:** CV's S3 URLs are auth-gated, so the plugin resolves a short-lived signed download URL via `GET /attachments/signedurl/:attachment_id` per attachment before fetching the bytes. No operator action required — uses the same `CARBONVOICE_PAT` you already configured.

**Size cap:** Set via `CARBONVOICE_MAX_ATTACHMENT_MB` (default `10`). Anything larger is dropped with a log line so the agent doesn't blow through vision-API token budgets on a 50 MB screenshot. Raise the cap for specialized workflows.

#### Upstream dependency (Hermes core)

Voice-out lands cleanly only when Hermes core honors two contracts the plugin relies on:

- `play_tts(...)` must propagate `reply_to` so the TTS audio threads under the user's message (instead of arriving as a top-level post).
- Hermes core's `_tts_caption_delivered` check must accept `adapter.voice_out_carries_text = True` to suppress the duplicate text bubble — CV's server-side STT already provides the transcript inside the voice-memo bubble, so sending the same text again as a separate bubble is pure noise.

Both contracts are pending upstream review (PR against `NousResearch/hermes-agent`). Until that PR merges, run the patched `base.py` locally — without it, voice-out still produces audio but you'll see duplicate text bubbles and top-level (non-threaded) replies. The plugin itself works correctly regardless.

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

**`deny-by-default is active but NO authorized users` warning** — `whoami` returned no owner (`created_by`) and `CARBONVOICE_ALLOWED_USERS` is empty, so the bot will ignore everyone. Set `CARBONVOICE_ALLOWED_USERS=<your_guid>` (or `CARBONVOICE_ALLOW_ALL_USERS=true` to disable gating). Normally the owner is auto-detected and this never fires.

**Messages from voice notes don't arrive** — transcription can take a few seconds. The adapter waits for `message:updated` (or the next poll) to pick up the populated transcript. If a transcript never arrives, check the Carbon Voice account has transcription enabled.

**State file getting out of sync** — delete `$HERMES_HOME/state/carbonvoice.json` to reset the cursor (the next start will pick up from "now").

## License

MIT
