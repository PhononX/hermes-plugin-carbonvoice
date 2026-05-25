# Development Notes

Internal documentation for contributors. The [README](README.md) is for operators who install and configure the plugin; this file is for engineers extending it.

## 1. Current state

The plugin connects a Hermes Agent instance to Carbon Voice as a bot user. It listens for inbound messages over Socket.IO (with REST polling as a fallback), routes them through a mention-aware gate, and dispatches accepted messages to the Hermes agent. The agent's reply is delivered back to Carbon Voice via REST.

### What works today

| Capability | Notes |
|---|---|
| Inbound via Socket.IO | Primary transport. Reconnects with exponential backoff. |
| Inbound via REST polling | Fallback when `python-socketio` is unavailable or the WS connection drops. |
| Offline catch-up | Cursor persisted to `$HERMES_HOME/state/carbonvoice.json`; messages received while Hermes was down are re-fetched on startup. |
| Outbound replies | `POST /v3/messages/start` with `reply_to_message_id` for threading. Stale-anchor retry as top-level on CV 400. |
| Self-loop filter | Compares `creator_id` against the agent's own `user_guid` (resolved at startup via `/whoami`). |
| Sender allowlist | Optional restriction by `user_guid`. Default: open. |
| Visual ack reaction | Optional. Fires on receipt to give users sub-100ms feedback before the agent finishes thinking. |
| Mark-as-read | Optional. Clears the unread notification once the agent has attempted to handle the message. |
| Username resolution in logs | Cached for the process lifetime. |
| Audit log of dropped senders | One JSON line per rejection in `$HERMES_HOME/logs/carbonvoice-ignored-senders.log`. |
| **Chat-type discrimination** | DM vs group resolved per-channel via `GET /channel/{id}`. Cached for the process lifetime. |
| **Reply context** | When an inbound message has `parent_message_id`, the adapter fetches the parent and populates `MessageEvent.reply_to_text` so the agent sees the text it's replying to. |
| **Mention gate** | In group channels, the agent only responds when `@`-mentioned. DMs always pass. Bypass with `CARBONVOICE_FREE_RESPONSE_CHANNELS`; hard veto with `CARBONVOICE_IGNORED_CHANNELS`; global disable with `CARBONVOICE_REQUIRE_MENTION=false`. |
| **Forward-compat mention detection** | Today: parses `@[Display Name](user_guid)` inline syntax embedded by the Flutter client. Tomorrow (post cv-api PR): prefers `tagged_user_ids` from the message payload. Same helper, automatic switchover. |
| **Inline markup stripping** | The `@[name](guid)` syntax is replaced with `@name` before the transcript reaches the agent, so the LLM sees clean text instead of GUID noise. |

## 2. Module map

```
hermes/
├── adapter.py        Thin orchestrator. Implements BasePlatformAdapter
│                     (connect, disconnect, send, get_chat_info). Composes
│                     all other modules. ~400 lines; should stay thin.
│
├── api.py            CarbonVoiceAPI — async HTTP client. One method per
│                     CV endpoint we call. Raises on HTTP/network errors.
│
├── audit.py          AllowlistGate (env-driven sender allowlist) +
│                     IgnoredSenderLog (JSON-lines audit of rejections).
│
├── channels.py       ChannelCache — resolves channel_id → "dm" | "group"
│                     via GET /channel/{id}. Per-process cache, no TTL.
│
├── constants.py      Compile-time defaults (base URL, poll interval, etc).
│
├── dedupe.py         SeenCache — TTL set of message_ids to avoid
│                     re-dispatching during retries / replays.
│
├── gate.py           MentionGate — stateless decision policy. Reads env
│                     config at construction; evaluate() returns a
│                     GateDecision (process: bool, reason: str).
│
├── parse.py          Pure functions only. Payload-shape helpers
│                     (extract_transcript, extract_message_id, etc.),
│                     chat_type mapper, inline mention helpers.
│
├── reactions.py      ReactionService — visual ack on inbound. Discovers
│                     available reaction IDs at startup; pin one via env.
│
├── setup.py          Standard Python install entry.
│
├── state.py          Cursor — disk-persisted "last seen at" timestamp
│                     for offline catch-up. Debounced 5s flush.
│
├── transport.py      Socket.IO client + REST polling lifecycle. Calls
│                     a callback when there's something to fetch.
│
└── users.py          UserCache — resolves user_guid → display name via
                      GET /v3/users/{id}. Per-process cache, no TTL.
```

**Design principle:** the adapter is a coordinator. Each subsystem owns one responsibility and is independently testable. When a new responsibility appears (e.g., the mention gate), it gets its own module rather than swelling the adapter.

## 3. Configuration reference

Every variable is optional except `CARBONVOICE_PAT`. Listed by what they control.

### Identity & connectivity
| Variable | Default | Purpose |
|---|---|---|
| `CARBONVOICE_PAT` | _(required)_ | Personal Access Token for the agent's identity. |
| `CARBONVOICE_BASE_URL` | `https://api.carbonvoice.app` | API base URL. Override for staging/dev. |
| `CARBONVOICE_POLL_INTERVAL_MS` | `5000` | REST polling interval when WS is unavailable. |
| `CARBONVOICE_WS_RETRY_MAX_MS` | `30000` | Max backoff for WS reconnects. |
| `CARBONVOICE_STATE_PATH` | `$HERMES_HOME/state/carbonvoice.json` | Cursor file path. |

### Sender allowlist (existing)
| Variable | Default | Purpose |
|---|---|---|
| `CARBONVOICE_ALLOWED_USERS` | _(unset)_ | Comma-separated `user_guid`s allowed to trigger the bot. |
| `CARBONVOICE_ALLOW_ALL_USERS` | `true` | When `false` and `ALLOWED_USERS` is empty, the bot is fully closed. |
| `CARBONVOICE_CREATOR_ID` | _(unset)_ | Legacy single-user restriction. |
| `CARBONVOICE_IGNORED_SENDERS_LOG` | `$HERMES_HOME/logs/carbonvoice-ignored-senders.log` | Audit log path. |

### Mention gate (new)
| Variable | Default | Purpose |
|---|---|---|
| `CARBONVOICE_REQUIRE_MENTION` | `true` | Require `@`-mention in group channels. Set `false` to disable gating entirely. |
| `CARBONVOICE_FREE_RESPONSE_CHANNELS` | _(unset)_ | Comma-separated `channel_guid`s where mention is not required. |
| `CARBONVOICE_IGNORED_CHANNELS` | _(unset)_ | Comma-separated `channel_guid`s where the agent never responds, even when mentioned. Hard veto, applies to DMs. |

### UX / behavior
| Variable | Default | Purpose |
|---|---|---|
| `CARBONVOICE_REACTION_ID` | `acknowledged` | Reaction id used for the visual ack. |
| `CARBONVOICE_DISABLE_ACK_REACTION` | `false` | Disable the visual ack. |
| `CARBONVOICE_DISABLE_MARK_READ` | `false` | Disable clearing the unread notification. |
| `CARBONVOICE_HOME_CHANNEL` | _(unset)_ | Default channel for cron/notification delivery. |
| `CARBONVOICE_HOME_CHANNEL_NAME` | _(unset)_ | Display name for the home channel. |

## 4. Known limitations

| Limitation | Impact | Resolution path |
|---|---|---|
| **Spoken-only mentions undetectable.** A user who records a voice memo *saying* "Hey Hermes" without typing `@` or using the tagging UI produces a transcript with no `@[name](guid)` markup and no entry in `tagged_user_ids`. The mention is invisible to the gate. | Voice users in group channels who don't use the tagging UI can't reach the bot. | No resolution planned for v1 — would require CV to detect mentions in spoken audio server-side. Voice memos *with* UI tagging work today via the `tagged_user_ids` field (deployed Q2 2026). |
| **No multi-user awareness in group channels.** Each user has an isolated session with the bot (`group_sessions_per_user=true` by default). The agent cannot reference what other participants said — it can't say "as Thomas mentioned earlier…" because Thomas's messages never enter Cristian's session history. | In group conversations the bot replies one-to-one even though humans perceive it as a multi-party discussion. | Set `SessionSource.thread_id` to the lane anchor; threads default to `thread_sessions_per_user=false` so all participants share one session. Hermes core then prefixes every user message with `[sender name]` ([run.py:6704](https://github.com/NousResearch/hermes-agent/blob/main/gateway/run.py#L6704)) and the agent attributes accordingly. Pairs with thread memory below — both need the same thread_id computation. |
| **No thread memory.** Every message in a group channel requires re-mention. A user mentioning the bot, then sending a follow-up in the same thread without re-mentioning, gets silence. | Conversational UX in groups is choppy. | Next-branch feature. Track engaged thread roots + outbound message ids in adapter state, pass booleans to the gate. Design sketched in §5. |
| **No deep-thread anchor walking.** Threads of depth >2 may mis-identify the "thread root." | Rare in CV in practice; affects nested reply chains. | Acceptable for v1 of thread memory. Resolution would be caching `parent_id → root_id` mappings or fetching the chain on first encounter. |
| **Text-only.** Voice messages are transcribed before delivery; attachments (`audio_url`, `attachments[]`) are not surfaced to the agent. | Agent can't "see" images, original audio, or document content. | Requires implementing `media_urls` / `media_types` in `_process_message`, downloading attachments via Hermes' `cache_*_from_url` helpers. Scope decision pending. |
| **No streaming replies.** `edit_message()` is not implemented; the agent's reply is delivered as a single complete message after thinking. | Long-running responses feel laggy with no "thinking" indicator. | Requires CV backend support for `PATCH /v3/messages/{id}` (verify) plus `edit_message()` override in the adapter. |
| **No interrupt support.** `/stop`, `/new`, `/reset` commands from CV won't cancel an in-flight agent run. | Users can't abort runaway responses. | Implement `interrupt_session_activity()`. |
| ~~**`tagged_user_ids` not in API response.**~~ ✅ **Resolved (Q2 2026).** The DB field is now surfaced in `Message`, `MessageV2`, and `MessageV5` DTOs. The plugin's forward-compat `is_user_mentioned()` automatically prefers the structured field over inline parsing. | Voice memos with UI tagging now reach the bot. | — |

## 5. Roadmap

Ordered by user-visible value per unit of effort. Items in **bold** are the next natural step.

### Short term (next branch)
1. **Thread memory.** Stateful tracking in the adapter: `_engaged_threads: set[str]` (thread roots where bot has been mentioned or replied) and `_outbound_message_ids: set[str]` (bot's own messages, for "reply to bot" detection). Gate stays stateless; adapter passes `is_engaged_thread` and `is_reply_to_bot` as inputs. Bounded with LRU eviction (~1000 entries). Adds env var `CARBONVOICE_STRICT_MENTION` to disable the shortcuts and demand re-mention every turn.
2. **Multi-user awareness in group channels.** Set `SessionSource.thread_id` to the lane anchor (`parent_message_id` for replies, `message_id` for top-level messages). With `thread_sessions_per_user=false` (the default in Hermes core), all participants in a thread share one session and Hermes core prefixes every user message with `[sender name]` ([run.py:6704](https://github.com/NousResearch/hermes-agent/blob/main/gateway/run.py#L6704)). The agent can then attribute statements ("as Thomas mentioned earlier…", "Cristian asked about X, Travis added Y, so the answer is Z"). Pairs naturally with thread memory above — both need the same lane-anchor computation, so they should ship in one branch. Optional env var `CARBONVOICE_SHARED_GROUP_SESSIONS=true` for the broader/simpler variant that bypasses thread resolution and flips `group_sessions_per_user` to false globally instead — useful for channels dedicated to bot collaboration where isolation isn't desired.
3. **`get_message()` caching.** Currently every reply fetches the parent on demand. A small LRU cache (~128 entries) cuts cost for star-shaped threads where many replies share a root.

### Medium term
4. **Lifecycle hooks refactor.** Move `mark_read` (currently in `_dispatch.finally`) to `on_processing_complete()`, and move `reaction.ack` to `on_processing_start()`. No behavior change; aligns the plugin with the convention used by Slack/Discord/Telegram adapters and shrinks `_process_message`. See [base.py:2346](https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/base.py#L2346) for the hook signatures.
5. **Per-channel system prompts.** Surface `MessageEvent.channel_prompt` by reading an env-driven map (`CARBONVOICE_CHANNEL_PROMPTS=guid:"you are support",guid2:"you are sales"`). Hermes core injects it as an ephemeral system prompt per message.
6. **Media support.** Populate `MessageEvent.media_urls` and `.media_types` from `attachments[]` and `audio_url`, using Hermes' `cache_image_from_url` / `cache_audio_from_url` helpers. Decision needed: do we break the "text-only" contract documented in the README?

### Long term (requires backend coordination or larger scope)
7. **Output rich.** Implement `edit_message()` for streaming, `send_voice()` for TTS replies, `send_image()` / `send_document()` for multimodal outputs.
8. **Interrupt support.** Implement `interrupt_session_activity()` so `/stop`, `/new`, `/reset` from CV cancel the in-flight agent task.
9. **Per-workspace skill bindings.** Map workspace_id → skill set so the agent loads different tools depending on which workspace the message came from.

### Blocked on backend
10. ~~**Migrate `is_user_mentioned()` to prefer `tagged_user_ids`.**~~ ✅ **Resolved (Q2 2026).** Forward-compat path was already coded, and cv-api now ships the field. No-op migration — already in effect. Voice memos with UI tagging reach the bot today.
11. **Enriched mention metadata.** If cv-api eventually returns `tagged_users: { id, display_name }[]` instead of raw IDs, the adapter could pass display names through to the agent for nicer replies ("@user1, ...").

## 6. Architecture decisions

Brief notes on the non-obvious choices. ADR-lite format.

### Composition over inheritance in the adapter
**Context.** `BasePlatformAdapter` is a ~60-method class in Hermes core (we don't control it). Implementing every method directly would balloon `adapter.py`.

**Decision.** The adapter implements only the 4 abstract methods (`connect`, `disconnect`, `send`, `get_chat_info`) plus `send_typing` (no-op). All other responsibilities live in single-purpose modules (`api`, `transport`, `state`, `dedupe`, `gate`, etc.) that the adapter composes.

**Consequence.** `adapter.py` stays under ~400 lines. Each subsystem is independently testable. Adding a new behavior usually means adding a new module, not modifying the adapter beyond a one-line wire-up.

### Stateless gate, stateful adapter
**Context.** The mention gate could carry per-channel state (engaged threads, bot message IDs). Putting state in the gate would couple decision policy with data-shape knowledge.

**Decision.** `MentionGate` is stateless and takes booleans as inputs (`is_reply_to_bot`, `is_engaged_thread` — pending). The adapter owns mutable state because it understands CV's data shapes (how to extract `parent_message_id`, where to record outbound IDs after `send()`).

**Consequence.** Gate is a pure function easy to unit-test. State changes don't risk breaking the decision policy. Adding new gate rules is local to `gate.py`; adding new state is local to `adapter.py`.

### Inline mention parsing as v1, structured field as v2
**Context.** Carbon Voice's DB has `tagged_user_ids` but it's not exposed in any API response. The Flutter client embeds mentions inline as `@[name](guid)` in the transcript. Two ways to detect mentions: parse the inline syntax, or wait for the backend to expose the field.

**Decision.** Implement inline parsing now. Code `is_user_mentioned()` to prefer `tagged_user_ids` when present and fall back to inline parsing when not. This is forward-compatible: when the backend ships the field, detection upgrades automatically with no plugin code change.

**Consequence.** Mention gate ships today. Voice-only mentions (no inline markup) are unsupported until the backend lands the field. Migration is zero-touch.

### `ChannelCache` has no TTL
**Context.** Channel kind (DM vs group) is effectively immutable after creation. A channel won't switch from DM to group mid-conversation.

**Decision.** Cache for the process lifetime. No TTL, no eviction. Memory grows linearly with unique channels encountered, which is bounded by user activity (typically dozens to hundreds, not millions).

**Consequence.** Simpler code, fewer API calls. If a channel were ever retyped (which CV doesn't allow), a process restart would pick up the new type.

### Strip `@[name](guid)` before the agent sees text
**Context.** The raw transcript contains GUIDs as part of mention markup. LLMs treat hex strings as noise that can confuse instruction following.

**Decision.** `strip_inline_mentions()` replaces `@[Display Name](guid)` with `@Display Name` before constructing the `MessageEvent`. The original raw payload is preserved in `event.raw_message` for any downstream code that needs it.

**Consequence.** Agent sees readable text. Same pattern Slack/Discord adapters use (stripping `<@U123>` to `@username`). No information loss because mention detection runs before stripping.

### Visual ack runs after the gate, not before
**Context.** Earlier behavior: ack every inbound message immediately on arrival. With the gate added, this would mean acking messages we then silently drop, which is confusing UX ("the bot saw my message but didn't reply").

**Decision.** Move the gate evaluation before the visual ack. If the gate rejects, no ack fires.

**Consequence.** Users in group channels who don't mention the bot see no reaction at all (matching their intent: they weren't talking to the bot). Users who do mention the bot see the ack within 100ms followed by the reply. Cleaner signal-to-noise.
