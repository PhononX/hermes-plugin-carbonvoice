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
| ~~**No multi-user awareness in group channels.**~~ ✅ **Resolved within threads (Q2 2026 PR 2).** `SessionSource.thread_id` is now populated from `ConversationTracker.thread_id_of(msg)` for groups, so all participants in a thread share one session and Hermes core prefixes each user message with `[sender name]` automatically. The agent can attribute statements across users in the same thread. **Outside threads** (top-level posts in a group), per-user isolation remains the default — set `CARBONVOICE_SHARED_GROUP_SESSIONS=true` to extend sharing to non-threaded conversations as well (useful for bot-room channels). | Bot now sees the full multi-party discussion when participants reply within a thread. | — |
| **No thread memory.** Every message in a group channel requires re-mention. A user mentioning the bot, then sending a follow-up in the same thread without re-mentioning, gets silence. | Conversational UX in groups is choppy. | Next-branch feature. Track engaged thread roots + outbound message ids in adapter state, pass booleans to the gate. Design sketched in §5. |
| ~~**No deep-thread anchor walking.**~~ ✅ **Not applicable to Carbon Voice.** CV enforces flat replies — the Flutter client's [`Message.getTopLevelGuid()`](https://github.com/PhononX/carbon-voice-flutter/blob/main/packages/cv_domain/lib/message/models/message.dart) returns `parent_message_id` (or self if top-level) without walking, and the [send queue](https://github.com/PhononX/carbon-voice-flutter/blob/main/packages/cv_data/lib/message/message_send_queue.dart) explicitly redirects any reply targeting a non-top-level message back to its parent. Backend rejects depth-2+ replies with `400 "You cannot reply to a message that is a reply"`. So `parent_message_id` is always the true thread root — no walking, no cache needed in the plugin. | — | — |
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

---

## 7. Session context — Built-in pattern alignment (next steps)

This section captures the working plan agreed in the design discussion that
preceded the next development push. It supersedes the earlier high-level
roadmap in §5 where they overlap; §5 remains as historical reference.

**Goal.** Bring the plugin in line with the conventions used by Hermes' own
built-in adapters (Slack, Discord, Telegram). Reference points:
- `gateway/platforms/base.py` in NousResearch/hermes-agent (the
  `BasePlatformAdapter` contract, ~4100 lines)
- `gateway/session.py` (`SessionSource`, `build_session_key`,
  `is_shared_multi_user_session`)
- <https://hermes-agent.nousresearch.com/docs/user-guide/messaging/>

### 7.1 Cross-reference with `BasePlatformAdapter`

Already aligned:
- 4 abstract methods implemented (`connect`, `disconnect`, `send`,
  `get_chat_info`) plus `send_typing` no-op.
- Composition-over-inheritance pattern (one responsibility per module).
- DM-vs-group mention split matching Slack/Discord/Telegram.
- Inline markup stripping (`@[name](guid)` → `@name`), same shape as
  Slack's `<@U123>` → `@username` pattern.
- Allowlist + audit log, mirroring `*_ALLOWED_USERS` / `GATEWAY_ALLOW_ALL_USERS`.
- Cursor-based offline catch-up.
- Stale-anchor recovery on outbound 400s.

Not yet implemented, ordered by impact-per-effort:

| Method / capability | BasePlatformAdapter site | Priority | Notes |
|---|---|---|---|
| `on_processing_start` / `on_processing_complete(outcome)` | base.py:2602–2606 | **short** | Pure refactor. Move `reaction.ack` and `mark_read` here. Unblocks tri-state reactions (👀 → ✅/❌). |
| `SessionSource.thread_id` populated | session.py:600 (`build_session_key`) | **short** | Without this, group channels stay user-isolated. See §7.3. |
| `interrupt_session_activity(session_key, chat_id)` | base.py:2502 | medium | Needed for `/stop`, `/new`, `/reset` to cancel an in-flight run. |
| `edit_message(chat_id, message_id, content, finalize=)` | base.py:1744 | medium | The big UX gap. Required for `GatewayStreamConsumer` to render progressive responses. Depends on CV exposing `PATCH /v3/messages/{id}`. |
| `delete_message` | base.py:1773 | medium | Enables `EphemeralReply` auto-deletion of system notices. |
| `MessageEvent.channel_prompt` | base.py:1073 | medium | Per-channel ephemeral system prompts (Discord pattern). |
| `format_message` | base.py:3985 | low | Override if CV doesn't render markdown — strip `**`/backticks so the LLM's formatting doesn't bleed through. |
| Media in (`media_urls` / `media_types`) | base.py:1060 | long | Breaks current text-only contract; product decision. |
| `send_voice`, `send_image`, `send_document`, `send_animation`, `send_image_file` | base.py:2038–2230 | long | CV being voice-first makes `send_voice` particularly high-value. |
| `play_tts` + auto-TTS plumbing | base.py:2156 | long | Hermes core already gates this via `_should_auto_tts_for_chat`. |
| `create_handoff_thread` | base.py:1717 | optional | Only if CV grows native sub-threads. |
| `send_clarify` / `send_slash_confirm` with inline buttons | base.py:1852, 1887 | skip | CV has no native button UI; text fallback works. |
| `send_draft` / `supports_draft_streaming` | base.py:1471, 1490 | skip | Telegram-specific. |

### 7.2 How Hermes sessions actually work (the mental model)

Sessions are persisted conversations (SQLite at `~/.hermes/state.db`). Each
inbound message is routed to a session via a deterministic `session_key`
that **the adapter does not choose directly** — `build_session_key()`
(`gateway/session.py:600`) composes it from `SessionSource` fields.

Key recipe:
```
DM:
  agent:main:<platform>:dm:<chat_id>
  agent:main:<platform>:dm:<chat_id>:<thread_id>      # if thread_id present

Group/channel:
  agent:main:<platform>:<chat_type>:<chat_id>[:<thread_id>][:<user_id>]
```

Two `gateway.json` flags govern whether `user_id` is appended:

| Flag | Default | Effect |
|---|---|---|
| `group_sessions_per_user` | `true` | In groups *without* a thread, each user gets an isolated session. |
| `thread_sessions_per_user` | `false` | In groups *with* a thread, all participants **share** one session. |

Net result by scenario (defaults):

| Scenario | Resulting key | Shared? |
|---|---|---|
| DM | `…:dm:<channel_id>` | n/a |
| Group, no thread_id | `…:group:<channel_id>:<user_id>` | ❌ per-user |
| Group, with thread_id | `…:group:<channel_id>:<thread_id>` | ✅ shared |

When the key resolves to a shared session, Hermes core
(`gateway/run.py:6704`) automatically prefixes every user message with
`[<user_name>]` before passing to the LLM, so the agent can attribute
statements ("Alice asked X, Bob added Y…"). The plugin only needs
`source.user_name` populated — `UserCache` already does this.

### 7.3 Current behavior in this plugin

Today `SessionSource.thread_id` is never set (`adapter.py:380`):

- **DMs:** `…:dm:<channel_id>` — correct, one session per user-bot pair.
- **Group channels:** `…:group:<channel_id>:<user_id>` — each user has an
  isolated session in the same channel. The agent cannot reference what
  other participants said. This is the "no multi-user awareness" limitation
  listed in §4.

### 7.4 Target behavior

For group channels, compute a "lane anchor" from CV's reply tree and use it
as `thread_id`. Because CV enforces flat replies (see §4 — frontend redirects
non-top-level replies via `getTopLevelGuid()`, backend rejects depth-2+ with
HTTP 400), `parent_message_id` is **always the true thread root**. No walking
or caching is required:

```python
# Lane anchor: top-level messages are their own root; replies use parent_message_id,
# which CV guarantees is the top-level root (never a nested reply).
thread_id = parent_message_id if parent_message_id else message_id

source = SessionSource(
    platform=Platform("carbonvoice"),
    chat_id=channel_id,
    chat_type=chat_type,                # "dm" or "group"
    user_id=creator_id,
    user_name=user_name,
    thread_id=thread_id,                # ← the new bit
    message_id=message_id,
)
```

This makes group threads behave like Slack/Discord/Telegram threads:
shared session, per-message sender attribution.

### 7.5 Required infrastructure: `conversations.py` (new module)

Five eyes of mutable state live in the adapter today; some are missing,
some are misplaced. Consolidate them in a new `ConversationTracker`:

| State axis | Granularity | Lifetime | Current location | Action |
|---|---|---|---|---|
| Cursor (`last_seen_at`) | global | persistent (disk) | `state.py` | keep |
| Self user id | global | process | `adapter.py` | keep |
| Channel type (DM/group) | per-channel | process | `channels.py` (`ChannelCache`) | keep |
| Display names | per-user | process | `users.py` (`UserCache`) | keep |
| Seen messages (dedup) | per-message | TTL ~10m | `dedupe.py` (`SeenCache`) | keep |
| Outbound reply anchor | **per-thread** | process | `adapter._last_inbound_msg` keyed by `channel_id` ⚠️ | **move + rekey** |
| ~~Thread root (parent→root)~~ | ~~per-message~~ | ~~process, LRU~~ | ~~—~~ | ~~**add**~~ — **not needed**, CV is flat (§4) |
| Engaged threads | per-thread | TTL ~30m, LRU | — | **add** |
| Outbound message ids (bot's own) | per-message | LRU ~1000 | — | **add** |
| Parent transcript cache | per-message | LRU ~128 | — | **add** |

Sketch:

```python
# conversations.py

class ConversationTracker:
    """All per-thread conversation memory for the adapter."""

    def __init__(
        self,
        *,
        max_thread_roots: int = 1000,
        engagement_ttl_s: int = 1800,        # ~30 min, ≈ Hermes idle reset
        max_outbound_ids: int = 1000,
        max_parent_text: int = 128,
    ):
        ...

    # Thread resolution — synchronous one-liner because CV enforces flat
    # replies (§4): `parent_message_id` is always the true thread root, no
    # API calls or walking needed.
    @staticmethod
    def thread_id_of(msg: dict) -> str: ...

    # Engagement
    def mark_engaged(self, thread_id: str) -> None: ...
    def is_engaged(self, thread_id: str) -> bool: ...     # TTL-aware
    def clear_engagement(self, thread_id: str) -> None: ...

    # Outbound tracking (for is_reply_to_bot)
    def record_outbound(self, message_id: str) -> None: ...
    def is_bot_message(self, message_id: str) -> bool: ...

    # Reply anchor (outbound threading) — keyed by thread_id, not channel_id
    def get_reply_anchor(self, thread_id: str) -> Optional[str]: ...
    def set_reply_anchor(self, thread_id: str, msg_id: str) -> None: ...

    # Parent text cache
    async def get_parent_text(
        self, parent_id: str, api: CarbonVoiceAPI
    ) -> Optional[str]: ...
```

### 7.6 Latent bug to fix while we're in there

`adapter._last_inbound_msg: Dict[channel_id → msg_id]` is keyed by
`channel_id`. Two concurrent threads in the same channel trample each
other's reply anchor. Rekey to `thread_id` as part of the migration into
`ConversationTracker`.

### 7.7 State persistence policy

Principle: persist only what is expensive to rebuild **and** affects UX
during restart.

| State | Persist? | Reason |
|---|---|---|
| Cursor | ✅ (already) | Without it we miss or duplicate offline messages. |
| Self user id, ChannelCache, UserCache | ❌ | Trivially refetchable on connect. |
| SeenCache | ❌ | Short TTL, refetch is idempotent. |
| Engaged threads | 🟡 v1 in-memory, revisit | Lose engagement on restart → one extra mention required. Acceptable for v1. |
| Outbound msg ids | 🟡 v1 in-memory, revisit | Loses "reply-to-bot" detection for pre-restart messages. Acceptable. |
| Thread roots, parent text | ❌ | LRU caches, refetch is cheap. |
| Reply anchors | ❌ | First post-restart reply goes top-level. Acceptable. |

If real-world dogfooding shows restart amnesia hurts UX, add persistence
to `state.py` with the same debounced-flush pattern used by `Cursor`. Do
not pre-optimize.

### 7.8 Coordination with Hermes core session resets

The plugin's `engaged_threads` and Hermes core's session reset policy
(`idle` / `daily`) can drift: engagement says "continue without mention"
but the underlying session has been reset to empty.

Resolution chosen: refresh engagement in `on_processing_complete`. With
TTL ≈ 30 min (close to typical Hermes idle reset) the drift window is
small. Accept the rare edge case rather than coupling the plugin to core
config.

### 7.9 Open design decisions

| Decision | Options | Default chosen | Revisit when |
|---|---|---|---|
| Thread root resolution | (a) direct parent, (b) walk-up with cache | **(a) — there is no (b)**: CV enforces flat replies, `parent_message_id` IS the root (§4) | — (CV's data model would have to fundamentally change) |
| Engagement TTL | 30m / 1h / 4h / no TTL | **30 min** | observe operator complaints about re-mentioning |
| Engagement persistence | in-memory / disk | **in-memory** | restart frequency increases |
| Reply anchor key | channel_id / thread_id | **thread_id** | — (fix the latent bug) |
| `CARBONVOICE_SHARED_GROUP_SESSIONS` env | yes / no | **yes, opt-in, off by default** | — |
| Subscribe to core session-reset events | yes / no | **no, accept drift** | core exposes a hook |
| `CARBONVOICE_STRICT_MENTION` env | yes / no | **yes, opt-in, off by default** | — |

### 7.10 PR sequence

Mechanical first, behavior-changing later. Each PR is independently
reviewable and shippable.

**PR 1 — refactor, no UX change.**
- Create `conversations.py` with `ConversationTracker`.
- Migrate `adapter._last_inbound_msg` → `tracker.reply_anchors` (rekeyed
  to `thread_id`, fixes latent bug from §7.6).
- Migrate `adapter._resolve_parent_text` → `tracker.get_parent_text`
  (parent text LRU cache from §7.5, addresses §5 item #3).
- Unit tests for the tracker (LRU bounds, TTL eviction).

**PR 2 — session sharing in group channels.**
- Compute `thread_id = parent_message_id or message_id` in
  `_process_message`.
- Pass `thread_id` into `SessionSource`.
- Add `CARBONVOICE_SHARED_GROUP_SESSIONS` env (global override that flips
  `group_sessions_per_user=false` for CV only — for bot-room channels).
- Validate manually with two accounts replying in one thread; confirm
  Hermes core's `[sender]` prefix appears and the agent attributes.

**PR 3 — thread memory + reply-to-bot.**
- `tracker.mark_engaged(thread_id)` after a successful dispatch (in the
  `on_processing_complete` hook).
- `tracker.record_outbound(msg_id)` after `send()` succeeds.
- Extend `MentionGate.evaluate(...)` with `is_engaged_thread` and
  `is_reply_to_bot` inputs (gate stays stateless; adapter passes them in).
- Add `CARBONVOICE_STRICT_MENTION` env (opt-in, forces re-mention every
  turn).

**PR 4 — lifecycle hooks refactor.**
- Move `reactions.ack` call to `on_processing_start(event)`.
- Move `mark_read` call to `on_processing_complete(event, outcome)`.
- Optional: tri-state ack reactions (`acknowledged` → swap to `done` /
  `failed` based on `ProcessingOutcome`).
- Shrink `_process_message` / `_dispatch` accordingly.

PR 1 and 2 are the foundation; PR 3 is where group-channel UX visibly
improves; PR 4 is technical hygiene. Beyond PR 4, the medium- and
long-term items from §7.1 (edit_message, interrupt_session_activity,
channel_prompt, media) become unblocked.

### 7.11 What to start with on the next session

Begin with **PR 1**. It is pure refactor (no observable behavior change),
fixes one latent bug (§7.6), and creates the structural home for
everything in PR 2 and PR 3. Suggested first prompt for a fresh Claude
session:

> Read `DEVELOPMENT.md` §7 in full, then implement PR 1 from §7.10:
> create `conversations.py` with `ConversationTracker` (sketch in §7.5),
> migrate `adapter._last_inbound_msg` and `adapter._resolve_parent_text`
> into it, rekey reply anchors to `thread_id` (currently `channel_id` —
> see §7.6), and add unit tests. Do not change any user-visible
> behavior. Run existing tests and report.
