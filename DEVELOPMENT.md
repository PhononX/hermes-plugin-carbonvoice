# Development Notes

Internal documentation for contributors. The [README](README.md) is for operators who install and configure the plugin; this file is for engineers extending it.

## 1. Current state

The plugin connects a Hermes Agent instance to Carbon Voice as a bot user. It listens for inbound messages over Socket.IO (with REST polling as a fallback), routes them through a mention-aware gate, and dispatches accepted messages to the Hermes agent. The agent's reply is delivered back to Carbon Voice via REST.

### What works today

| Capability | Notes |
|---|---|
| Inbound via Socket.IO | Primary transport. Reconnects with exponential backoff. |
| Inbound via REST polling | Fallback when `python-socketio` is unavailable or the WS connection drops. |
| Offline catch-up | Cursor persisted to `$HERMES_HOME/state/carbonvoice.json`; messages received while Hermes was down are re-fetched on startup. Polls by `updated_at` (`use_last_updated`). **Stuck-message hold:** if a message's transcript isn't ready yet, `_process_message` returns `None` and the poll loop holds the cursor *just before* it (advances only to the previous message's `created_at`) so the next poll retries it instead of advancing past — borrowed from the Claude Code Channel's null-return contract. |
| Outbound replies | `POST /v3/messages/start` with `reply_to_message_id` for threading. Stale-anchor retry as top-level on CV 400. |
| Self-loop filter | Compares `creator_id` against the agent's own `user_guid` (resolved at startup via `/whoami`). |
| Sender allowlist (**deny-by-default**) | Only the auto-detected owner (`whoami.created_by`), `CARBONVOICE_ALLOWED_USERS`, and `/cv-allow-user`-approved users can talk to the bot. Unknown senders trigger an interactive approval prompt to the owner in the home channel. `CARBONVOICE_ALLOW_ALL_USERS=true` reopens it. |
| Visual ack reaction | Optional. Fires on receipt to give users sub-100ms feedback before the agent finishes thinking. |
| Mark-as-read | Optional. Clears the unread notification once the agent has attempted to handle the message. |
| Username resolution in logs | Cached for the process lifetime. |
| Audit log of dropped senders | One JSON line per rejection in `$HERMES_HOME/logs/carbonvoice-ignored-senders.log`. |
| **Chat-type discrimination** | DM vs group resolved per-channel via `GET /channel/{id}`. Cached for the process lifetime. |
| **Reply context** | When an inbound message has `parent_message_id`, the adapter fetches the parent and populates `MessageEvent.reply_to_text` so the agent sees the text it's replying to. |
| **Mention gate** | In group channels, the agent only responds when `@`-mentioned. DMs always pass. Bypass with `CARBONVOICE_FREE_RESPONSE_CHANNELS`; hard veto with `CARBONVOICE_IGNORED_CHANNELS`; global disable with `CARBONVOICE_REQUIRE_MENTION=false`. |
| **Structured mention detection** | Detection is **exclusively** via the `tagged_user_ids` array on the message (cv-api #243 exposes it; #271/#278 populate it for text + voice). The Flutter composer strips mentions to plain `@Name` before send and tags voice memos via the batch `PUT /messages/:id/tagged-users` after STT — so the transcript carries no GUID markup. Nothing to parse or strip on the plugin side; voice tags that land after STT re-enter the gate via the `revisitable` re-fire path. |

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
├── audit.py          AllowlistGate (deny-by-default: owner + env +
│                     pairing) + IgnoredSenderLog (JSON-lines audit).
│
├── permits.py        ApprovalStore (dynamic allow-list backed by core's
│                     PairingStore) + /cv-* command parser. Powers the
│                     interactive owner-approval onboarding flow.
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
│                     chat_type mapper, structured mention check.
│
├── reactions.py      ReactionService — visual ack on inbound. Discovers
│                     available reaction IDs at startup; pin one via env.
│
├── setup.py          Standard Python install entry.
│
├── state.py          Cursor — disk-persisted "last seen at" timestamp
│                     for offline catch-up. Debounced 5s flush.
│
└── transport.py      Socket.IO client + REST polling lifecycle. Calls
                      a callback when there's something to fetch.
```

> **Name resolution lives in `channels.py`, not a separate `users.py`.**
> The standalone `GET /v3/users/{id}` endpoint is dead (404), so the old
> `UserCache` was removed. `ChannelCache` now derives both `chat_type` and
> a `{user_guid → name}` roster from one `GET /channel/{id}` call
> (`json_collaborators`), so participant names cost zero extra requests.

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

### Sender allowlist — **deny-by-default + interactive approval**
A user may reach the agent if **any** of: (1) they are the **owner**
(`whoami.created_by`, auto-detected at connect, always allowed); (2) they
are in `CARBONVOICE_ALLOWED_USERS`; (3) they were approved at runtime via
`/cv-allow-user` (stored in Hermes core's `PairingStore`, which the core
authorization also consults — so approving here authorizes in core too).
Default (no config) → only the owner. The owner approves others from the
home channel (owner-only commands): `/cv-allow-user <id>`,
`/cv-deny-user <id>`, `/cv-list-allow-users`.

| Variable | Default | Purpose |
|---|---|---|
| `CARBONVOICE_ALLOWED_USERS` | _(unset)_ | Extra `user_guid`s allowed, beyond the owner and `/cv-allow-user`-approved users. |
| `CARBONVOICE_ALLOW_ALL_USERS` | `false` | **Deny-by-default.** `true` disables gating entirely (old open behavior). Was the implicit `true` default before this change. |
| `CARBONVOICE_HOME_CHANNEL` | _(unset)_ | Where the bot asks the owner to approve unknown senders. Without it, unknown senders are denied silently (no prompt). |
| `CARBONVOICE_CREATOR_ID` | _(unset)_ | Legacy single-user restriction. |
| `CARBONVOICE_IGNORED_SENDERS_LOG` | `$HERMES_HOME/logs/carbonvoice-ignored-senders.log` | Audit log path. |

> **Migration / security note.** The default flipped from allow-all to
> **deny-all**. Existing deployments with an empty allow-list now answer
> only the owner until they `/cv-allow-user` others (or set
> `CARBONVOICE_ALLOW_ALL_USERS=true`). This closes the hole where anyone on
> a shared channel could drive the agent on the host. Warrants a version
> bump + CHANGELOG callout.

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

### Voice-out (PR 6) — opt-in, off by default

> **Product decision (Q2 2026): keep this off.** Carbon Voice does STT and any
> TTS on its own backend, so the agent replies in text and CV handles speech.
> The voice-out code is kept intact (not deleted) so it can be re-enabled by
> flipping `CARBONVOICE_VOICE_OUT=true` + `voice.auto_tts: true` — but the
> default is text-out and that is the supported path.

| Variable | Default | Purpose |
|---|---|---|
| `CARBONVOICE_VOICE_OUT` | `false` | When `true`, every inbound message is tagged `MessageType.VOICE` so Hermes core's auto-TTS pipeline (`gateway/platforms/base.py:3493`) converts the agent's text reply into audio and ships it via `send_voice` → `/v5/messages/audio`. CV transcribes the audio server-side, so the recipient sees a voice-memo bubble with the transcript inline. Required companion config in `~/.hermes/config.yaml`: `voice.auto_tts: true` plus a configured `tts.provider` (`edge` works key-less; `elevenlabs` / `openai` require their respective API keys). With voice-out off (default) the adapter preserves the existing text-reply behavior so existing deployments don't change shape unexpectedly. |

### Inbound multimodal (PR 7)
| Variable | Default | Purpose |
|---|---|---|
| `CARBONVOICE_MAX_ATTACHMENT_MB` | `10` | Per-attachment byte cap (in MB) for inbound files the plugin downloads and forwards to the agent's multimodal pipeline. Images, PDFs, and `text/*` files smaller than the cap are resolved through `GET /attachments/signedurl/:id`, downloaded to `~/.hermes/{image,document}_cache/`, and exposed via `MessageEvent.media_urls=['file://...']` so the agent sees them natively (vision for images, document parsing for PDFs/text). Anything larger gets logged at WARNING and skipped — the text part of the message still reaches the agent. Default 10 MB matches Claude / OpenAI vision recommendations and keeps token costs predictable. |

## 4. Known limitations

| Limitation | Impact | Resolution path |
|---|---|---|
| **Spoken-only mentions undetectable.** A user who records a voice memo *saying* "Hey Hermes" without using the tagging UI produces a message with no entry in `tagged_user_ids` — and since detection is structured-field-only, the mention is invisible to the gate. | Voice users in group channels who don't use the tagging UI can't reach the bot. | No resolution planned — would require CV to detect mentions in spoken audio server-side. Voice memos *with* UI tagging work today: the tag is applied after STT via the batch endpoint and the gate's `revisitable` re-fire picks it up. |
| ~~**No multi-user awareness in group channels.**~~ ✅ **Resolved within threads (Q2 2026 PR 2).** `SessionSource.thread_id` is now populated from `ConversationTracker.thread_id_of(msg)` for groups, so all participants in a thread share one session and Hermes core prefixes each user message with `[sender name]` automatically. The agent can attribute statements across users in the same thread. **Outside threads** (top-level posts in a group), per-user isolation remains the default — set `CARBONVOICE_SHARED_GROUP_SESSIONS=true` to extend sharing to non-threaded conversations as well (useful for bot-room channels). | Bot now sees the full multi-party discussion when participants reply within a thread. | — |
| **No thread memory.** Every message in a group channel requires re-mention. A user mentioning the bot, then sending a follow-up in the same thread without re-mentioning, gets silence. | Conversational UX in groups is choppy. | Next-branch feature. Track engaged thread roots + outbound message ids in adapter state, pass booleans to the gate. Design sketched in §5. |
| ~~**No engaged-thread context on first @mention.**~~ ✅ **Resolved (Q2 2026 PR 4).** When the agent is @mentioned in a thread for the first time (no Hermes session yet for that thread), `adapter._fetch_thread_context` pulls the prior messages and prepends them as a `[Thread context …]` block so the LLM has history from turn 1. Cached per-thread (TTL 30 min, LRU 200) so re-mentions in a hot thread don't re-hit the API. Subsequent turns ride on Hermes' SQLite session history. | The first @mention in a long-running thread now carries the prior conversation; no more "wait, what are we talking about?" responses. | — |
| **No native "list messages in thread" endpoint on cv-api.** PR 4's thread-context fetch combines two REST calls (`GET /messages/<channel_id>/index` for ids + `parent_message_id`, then `POST /v5/messages/by-ids` for transcripts) and filters client-side. Works, but pays the index fetch on every cold cache. | One extra index call per thread per TTL window. Negligible for low-volume workspaces; worth optimizing for high-volume ones. | Backend ask: `GET /v5/messages/<thread_id>/replies?limit=N` or extend `getMessageIdsV5` to accept `thread_id` filter. Collapses the workaround to a single call. |
| ~~**No deep-thread anchor walking.**~~ ✅ **Not applicable to Carbon Voice.** CV keeps threads flat (one level) — the Flutter client's [`Message.getTopLevelGuid()`](https://github.com/PhononX/carbon-voice-flutter/blob/main/packages/cv_domain/lib/message/models/message.dart) returns `parent_message_id` (or self if top-level) without walking, and the [send queue](https://github.com/PhononX/carbon-voice-flutter/blob/main/packages/cv_data/lib/message/message_send_queue.dart) redirects any reply targeting a non-top-level message back to its parent. **As of cv-api PR #277 (CV-13155) the backend itself now normalizes this server-side** (`resolveRootParentMessageId`): replying to a non-root message no longer returns `400 "You cannot reply to a message that is a reply"` — the server resolves and stores the thread root. (Cross-conversation replies still 400.) Stored `parent_message_id` is therefore always the true thread root — no walking, no cache needed in the plugin, and the client is free to pass any visible message id as `reply_to_message_id`. | — | — |
| **Text-only inbound (partially resolved).** ✅ **Images + link attachments work (Q2 2026 PR 7).** Inbound `attachments[]` of type `image/*` are downloaded via `GET /attachments/signedurl/:id` to `~/.hermes/image_cache/` and surfaced via `MessageEvent.media_urls` + `media_types` — Claude vision sees the bytes inline. `type:"link"` attachments (CV's link-share UI) have their URL prepended to the message text as `[Attached link: <url>]`, so the agent uses its own browser tool to fetch — same path as URLs typed inline. ❌ **PDFs and text files are dropped with a WARNING for now** because Hermes core has no native document-extraction pipeline; without one, the agent receives a `file://...pdf` path it can't read natively, and falls through to `read_file` → `terminal`/`execute_code` which trigger permission prompts on the operator side. Voice memo audio (in `audio_models[]`, not `attachments[]`) remains transcript-only — fetching raw audio for re-transcription / tone analysis is also future work. See `CARBONVOICE_MAX_ATTACHMENT_MB` in §3 for the size cap (default 10 MB). | Photos + link previews work end-to-end. PDFs / text attachments are skipped (text part of the message still reaches the agent). | Follow-up PR: add `pypdf` + a `text/*` reader in `_collect_inbound_media` and prepend extracted text to `clean_text` the same way thread context + link URLs are prepended today. |
| ~~**Local file attachments not supported on outbound.**~~ ✅ **Resolved (Q2 2026 PR 5).** `send_document` and `send_image` now accept either a URL or a local file path. Local files follow the Flutter client's flow: `POST /v3/attachments/signedurl` → direct S3 PUT → `POST /v5/messages/{text,attachment}` with `type:"file"` + `status:"Initializing"` → background `PUT /messages/{m}/attachment/{a}` to flip status to `Uploaded`/`Failed`. The agent gets `SendResult(success=True)` immediately; the recipient sees the bubble appear with a placeholder that resolves once S3 acks. URL inputs skip the upload and attach `type:"link"`. `send_voice` keeps using `/v5/messages/audio` (multipart, server-side transcription) — that's the right tool for voice memos; for plain audio attachments use `send_document` with an `.mp3`/`.wav` path. | The agent can now ship `.md` reports, PDFs, images, and arbitrary files generated locally — same UX as a Flutter user pressing the attach button. | — |
| **No streaming replies.** `edit_message()` is not implemented; the agent's reply is delivered as a single complete message after thinking. | Long-running responses feel laggy with no "thinking" indicator. | Requires CV backend support for `PATCH /v3/messages/{id}` (verify) plus `edit_message()` override in the adapter. |
| **No interrupt support.** `/stop`, `/new`, `/reset` commands from CV won't cancel an in-flight agent run. | Users can't abort runaway responses. | Implement `interrupt_session_activity()`. |
| ~~**`tagged_user_ids` not in API response.**~~ ✅ **Resolved (Q2 2026).** The DB field is now surfaced in `Message`, `MessageV2`, and `MessageV5` DTOs (cv-api #243). `is_user_mentioned()` is now a structured-field-only check — the inline-parsing fallback was removed once the Flutter client stopped embedding GUIDs in the transcript (see §6 ADR). | Voice memos with UI tagging now reach the bot. | — |
| ~~**`tagged_user_ids` empty on inbound socket push for voice messages with picker tags.**~~ ✅ **Resolved (Q2 2026 PR — V5 source-of-truth migration).** The WebSocket / `/v3/messages/recent` push delivers a V2-shaped payload that **trails** the v5 GET on async fields: the tag-resolution job that populates `tagged_user_ids` from the Flutter picker selection runs after the message is created and STT-transcribed, but the socket push doesn't wait for it. Querying `GET /v5/messages/:id` minutes later returns the populated array — so the field reaches v5 correctly, just not the push. The plugin now treats the socket / poll payload as a **notification signal** and refetches via `get_message_v5(id)` inside `_process_message` (right after the cheap-reject gates so empty-transcript events don't pay the HTTP). This matches the architecture the Flutter client already uses (signal-then-pull). Defensive fallback to the V2 payload on fetch failure so a transient `/v5` hiccup doesn't drop the message. Parse helpers (`extract_message_id`, `extract_channel_id`, `extract_transcript`) handle both V5 and V2 shapes so the pre-enrichment and webhook paths still work. | Voice-message tagging via the Flutter picker now reaches the bot reliably. Side benefit: any future field the V5 serializer adds to the GET endpoint is consumed automatically. | — |
| ~~**v5 outbound used the short-lived `thread_id` input field.**~~ ✅ **Resolved (Q2 2026 — cv-api PR #277 / CV-13155, cv-contracts 4.0.1).** The v5 *conversation* create routes (`/v5/messages/{text,audio,attachment}`) **renamed the threading input `thread_id` → `reply_to_message_id`** and moved `thread_id` into the v5 reject-deprecated-fields pipe — sending it now returns **400**. The backend also got smarter: `resolveRootParentMessageId` resolves whatever message id you pass to its thread root server-side (replying to a reply is normalized instead of 400'd; only cross-conversation replies still fail). On the **inbound** side, `MessageV5` and the webhook payload **dropped `thread_id`**; `parent_message_id` is the canonical (un-deprecated) public thread field again. The plugin's `api.send_text_v5` / `send_audio_v5` / `send_attachment_v5` now send `reply_to_message_id`; the adapter passes the resolved thread root (root-resolves-to-itself keeps threading correct). The Hermes-side `thread_id` *concept* (`SessionSource.thread_id`, `ConversationTracker.thread_id_of`, reply anchors, thread-context cache) is unchanged — it already reads inbound `parent_message_id`. CI locks the rename (the three send methods must expose `reply_to_message_id` and not `thread_id`). | Threaded replies / voice memos / attachments no longer 400 against current `main`. Clients may now reply to any visible message, not just the root. | — |
| ~~**Agent saw user IDs, not names.**~~ ✅ **Resolved (Q2 2026).** `GET /v3/users/{id}` is dead (404 for every guid, including the bot), so the old `UserCache` always fell back to the raw guid — and Hermes core's session context showed `**User ID:** <guid>` instead of a name. Names now come from the channel's `json_collaborators` (`ChannelCache.resolve_name`, same `GET /channel/{id}` call as chat_type → zero extra requests). `SessionSource.user_name` is populated for DMs and groups, and a participant roster is injected via `MessageEvent.channel_context` (`[Participants in this conversation: …]`) when there are ≥2 humans, so the agent can name and attribute people — including those who haven't spoken yet. | The agent knows who it's talking to and who else is in the room. | — |
| ~~**`/v5/messages/by-ids` 400'd → thread context silently broke.**~~ ✅ **Resolved (Q2 2026).** The endpoint's request body changed from `{ids:[...]}` to `{conversation_id, message_ids:[...]}` (it now requires the conversation id). `get_messages_by_ids_v5` sends the new shape and `_fetch_thread_context` passes `channel_id`; first-@mention thread context works again. Returned items are flat MessageV5 dicts (no `{"message": …}` envelope, unlike the single GET). | First-@mention thread history reaches the agent again. | The single-call thread-listing endpoint in the row above would still be a nice optimization. |
| ~~**Poll could skip a message whose transcript wasn't ready.**~~ ✅ **Resolved (Q2 2026).** The poll loop used to advance the cursor to the request start time regardless of whether every message had been delivered, so a message CV was still transcribing could be passed over (its `created_at` ends up behind the cursor; `use_last_updated` re-catches it only when the backend bumps `updated_at`). `_process_message` now returns `None` for a not-ready transcript (vs `False` for a permanent skip), and `_fetch_missed_messages` holds the cursor just before the first such message so the next poll retries it deterministically. Idea borrowed from the Claude Code Channel ([cv-claude-channels](https://github.com/PhononX/cv-claude-channels))'s null-return + stuck-message handling. | No message is skipped while its transcript is still being produced. | — |
| ~~**Same message re-processed (agent re-answered it multiple times).**~~ ✅ **Resolved (Q2 2026).** Dedup was in-memory only (`SeenCache`, 5-min TTL) — lost on every gateway restart and lapsing after 5 min. Combined with `use_last_updated` polling, this looped: the adapter's own **ack reaction** and **in-thread reply** both bump the message's `updated_at`, so the poller keeps re-fetching it; once the SeenCache lapses (or a restart clears it) the agent answers the same message again. Observed live: one message (`b0a39830…`, created 18:39) re-dispatched 5× across a day of restarts, its `updated_at` trailing each re-answer to 23:21. Fix: **server-side dedup via the ack reaction** — `_process_message` now checks `reaction_summary` (`parse.bot_has_reacted`) on the canonical v5 payload and skips any message the bot already acked. The reaction persists in CV, so this survives restarts and breaks the loop; the `SeenCache` stays as a fast first line (and covers the brief window before the ack is reflected). Mirrors the Claude Code Channel's reaction-based `isProcessed`. | The agent answers each message exactly once, even across restarts. | Requires the ack reaction enabled (default). With `CARBONVOICE_DISABLE_ACK_REACTION=true` there's no server-side marker and dedup falls back to the in-memory SeenCache only. |

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
10. ~~**Migrate `is_user_mentioned()` to prefer `tagged_user_ids`.**~~ ✅ **Resolved (Q2 2026).** Went further than "prefer" — detection is now `tagged_user_ids`-**only**. The Flutter client stopped embedding GUIDs in the transcript (sends plain `@Name` + the id array; voice tags via the batch endpoint after STT), so the inline-parsing fallback and `strip_inline_mentions` were deleted. Voice memos with UI tagging reach the bot today.
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

### Mention detection: structured `tagged_user_ids` only
**Context.** Originally Carbon Voice's `tagged_user_ids` was not exposed on any API response, so the plugin parsed the Flutter client's inline `@[name](guid)` transcript markup (with a forward-compat preference for the structured field "once the backend ships it"). The backend then shipped the field (cv-api #243), and the Flutter client was reworked (Q2 2026) so it now sends mentions **structured only**: the composer strips `@[name](guid)` → plain `@Name` before send (`MentionParser.stripToDisplay`) and carries the ids in `tagged_user_ids`, while voice memos tag via the batch `PUT /messages/:id/tagged-users` after recording. The transcript no longer contains GUID markup on any path.

**Decision.** Detect mentions **exclusively** from `tagged_user_ids`. The inline-parsing fallback and the `strip_inline_mentions` cleanup were deleted — there is no markup left to parse or strip. Voice is the reason the structured field is authoritative: the tag lands *after* STT on a `message:updated`, so detection cannot depend on anything present at create time.

**Consequence.** `is_user_mentioned()` is a one-line `tagged_user_ids` membership check. Voice memos reach the bot reliably because the gate's `revisitable` rejection (keeps the message out of the dedup cache) plus the `get_message_v5` enrichment re-evaluate the updated payload once the tag job populates the array. A user who *speaks* the agent's name without using the tagging UI is still undetectable (see §4) — unchanged.

### `ChannelCache` has no TTL
**Context.** Channel kind (DM vs group) is effectively immutable after creation. A channel won't switch from DM to group mid-conversation.

**Decision.** Cache for the process lifetime. No TTL, no eviction. Memory grows linearly with unique channels encountered, which is bounded by user activity (typically dozens to hundreds, not millions).

**Consequence.** Simpler code, fewer API calls. If a channel were ever retyped (which CV doesn't allow), a process restart would pick up the new type.

### ~~Strip `@[name](guid)` before the agent sees text~~ (removed Q2 2026)
**Superseded.** The plugin used to run the transcript through `strip_inline_mentions()` to turn `@[Display Name](guid)` into a readable `@Display Name` before building the `MessageEvent`. The Flutter client now does that stripping itself (`MentionParser.stripToDisplay`) and sends the transcript as plain `@Name`, so the inbound text is already clean — the helper and both call sites were deleted. The agent still sees readable `@Name` text (same end result as the old Slack-style `<@U123>` → `@username` cleanup); it just happens upstream now. See "Mention detection: structured `tagged_user_ids` only" above.

### Visual ack runs after the gate, not before
**Context.** Earlier behavior: ack every inbound message immediately on arrival. With the gate added, this would mean acking messages we then silently drop, which is confusing UX ("the bot saw my message but didn't reply").

**Decision.** Move the gate evaluation before the visual ack. If the gate rejects, no ack fires.

**Consequence.** Users in group channels who don't mention the bot see no reaction at all (matching their intent: they weren't talking to the bot). Users who do mention the bot see the ack within 100ms followed by the reply. Cleaner signal-to-noise.

---

## 7. Session context — Built-in pattern alignment (working plan)

This section captures the working plan that has guided the recent
development pushes. Sections marked ✅ have shipped (see commit history
on `main`); the rest is what remains. It supersedes the earlier
high-level roadmap in §5 where they overlap; §5 remains as historical
reference.

**Goal.** Bring the plugin in line with the conventions used by Hermes' own
built-in adapters (Slack, Discord, Telegram). Reference points:
- `gateway/platforms/base.py` in NousResearch/hermes-agent (the
  `BasePlatformAdapter` contract, ~4100 lines)
- `gateway/session.py` (`SessionSource`, `build_session_key`,
  `is_shared_multi_user_session`)
- <https://hermes-agent.nousresearch.com/docs/user-guide/messaging/>

### 7.0 Plan revision (2026-05-25)

The original §7.10 plan sequenced PR 3 as "thread memory + reply-to-bot
shortcuts" before tackling v5 endpoints. After dogfooding PR 1 + 2 and
checking with the CV backend team, that ordering was reversed:

1. The CV team's guidance ("Just always reply to `thread_id` when wanting
   to do thread — eliminate the client guessing") is encoded natively in
   the **v5 transport** (`POST /v5/messages/text` accepts `thread_id`
   directly). Migrating to v5 *removes* the need for the reply-anchor
   lookup that PR 1 introduced — `send()` becomes a one-liner pass
   through.
2. v5 also exposes `POST /v5/messages/stream`, which unblocks
   `edit_message()` — the "chain of thought becomes one growing bubble
   instead of N separate messages" UX win that visibly transforms the
   product. Higher visible value than thread-memory shortcuts.
3. The "engaged thread context" goal (bot has context of all messages
   since first mention in a thread) is *better* solved by fetching the
   thread on `@mention` from a v5 endpoint than by maintaining a local
   in-memory buffer — it survives gateway restarts and TTL expiry, and
   delivers the full thread history including pre-engagement messages.

So PR 3 is now **v5 transport + `edit_message`** (was: "thread memory").
Engaged-thread context fetching becomes PR 4. The mention-gate
shortcuts (was PR 3) become PR 5 and are *probably unnecessary* once
PR 4 lands — re-evaluated then.

### 7.1 Cross-reference with `BasePlatformAdapter`

Already aligned (✅ shipped):
- 4 abstract methods implemented (`connect`, `disconnect`, `send`,
  `get_chat_info`) plus `send_typing` no-op.
- Composition-over-inheritance pattern (one responsibility per module).
- DM-vs-group mention split matching Slack/Discord/Telegram.
- Structured mention detection via `tagged_user_ids` (the Flutter client
  sends plain `@Name` text + the id array; no inline markup to parse or
  strip — see the §6 ADR).
- Allowlist + audit log, mirroring `*_ALLOWED_USERS` / `GATEWAY_ALLOW_ALL_USERS`.
- Cursor-based offline catch-up.
- Stale-anchor recovery on outbound 400s (PR 2 closed the underlying
  root cause: `send()` now prefers `tracker.reply_anchors[thread_id]`
  over Hermes core's raw `reply_to`).
- `ConversationTracker` consolidates per-thread state (PR 1).
- `SessionSource.thread_id` populated for groups → shared sessions
  with `[sender name]` prefixing (PR 2).

Not yet implemented, ordered by **value-per-effort** with the
post-2026-05-25 priorities baked in:

| Method / capability | BasePlatformAdapter site | Priority | Notes |
|---|---|---|---|
| ~~v5 transport~~ | ~~api.py~~ | ~~PR 3~~ ✅ Shipped | `send_text_v5` / `send_audio_v5` / `send_attachment_v5` + `get_message_v5` / `get_messages_by_ids_v5` in api.py. `adapter.send` migrated; new `send_voice` / `send_image` / `send_document` overrides land via v5 endpoints. CV team's "always reply to thread_id" intent encoded — no client reply-anchor lookup. |
| `edit_message(chat_id, message_id, content, finalize=)` | base.py:1744 | **blocked on backend** | The big UX gap (chain-of-thought as one growing bubble vs N messages). v5 has no PATCH endpoint and `/v5/messages/stream` is for audio uploads, not text editing. The closest path — `PUT /v3/messages/transcript` — is meant for human voice-transcript corrections and requires shoehorning text into `WordsWithTimeCode` format. Needs a backend `PATCH /v5/messages/{id}` (or equivalent) before this can ship cleanly. |
| ~~Engaged-thread context via API fetch~~ | ~~(custom — adapter-level)~~ | ~~PR 4~~ ✅ Shipped | `adapter._fetch_thread_context` pulls the thread's prior messages on first `@mention` and prepends as `[Thread context …]` block. Combines `list_channel_message_index` + `get_messages_by_ids_v5` (workaround for the missing thread-listing endpoint — see §4). Cached per-thread (TTL 30 min, LRU 200); subsequent turns ride on Hermes' SQLite session. |
| ~~Local file attachments on outbound (`send_document`, `send_image`)~~ | ~~adapter.py~~ | ~~PR 5~~ ✅ Shipped | Signed-URL → S3 PUT → message-create with `type:"file"` + `status:"Initializing"` → background status update. Mirrors the Flutter client flow. Agent ships `.md`/PDFs/audio just like a human attaches files. |
| `on_processing_start` / `on_processing_complete(outcome)` | base.py:2602–2606 | **PR 6** | Pure refactor. Move `reaction.ack` and `mark_read` here. Unblocks tri-state reactions (👀 → ✅/❌). |
| `interrupt_session_activity(session_key, chat_id)` | base.py:2502 | medium | Needed for `/stop`, `/new`, `/reset` to cancel an in-flight run. |
| `delete_message` | base.py:1773 | medium | Enables `EphemeralReply` auto-deletion of system notices. |
| `MessageEvent.channel_prompt` | base.py:1073 | medium | Per-channel ephemeral system prompts (Discord pattern). |
| Thread memory + reply-to-bot shortcuts (was PR 3) | gate.py + tracker | **deferred → PR 7 (probably unnecessary after PR 4)** | Once PR 4 fetches thread history on every `@mention`, the engagement-bypass shortcut adds little value. Revisit after PR 4 dogfooding. |
| `format_message` | base.py:3985 | low | Override if CV doesn't render markdown — strip `**`/backticks so the LLM's formatting doesn't bleed through. |
| Media in (`media_urls` / `media_types`) | base.py:1060 | long | Breaks current text-only contract; product decision. |
| ~~`send_voice` (via v5 audio endpoint)~~ | ~~base.py:2038~~ | ~~PR 3~~ ✅ Shipped | Uses `/v5/messages/audio` for server-side transcription — the right tool for voice memos. Generic audio attachments (`.mp3` files etc.) flow through PR 5's `send_document`. |
| ~~`send_image`, `send_document`~~ | ~~base.py:2110–2230~~ | ~~PR 5~~ ✅ Shipped | Local file or URL both supported (see PR 5). `send_animation` / `send_image_file` remain unimplemented (low priority — CV doesn't distinguish them from generic file attachments). |
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
`source.user_name` populated — `ChannelCache.resolve_name` (from the channel roster) does this.

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
| Display names | per-channel roster | process | `channels.py` (`ChannelCache`, from `json_collaborators`) | keep |
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
| Self user id, ChannelCache (chat_type + roster) | ❌ | Trivially refetchable on connect. |
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

Foundation first, then the visibly-transformative UX changes, then
nice-to-haves. The original §7.10 sequencing (thread memory before v5)
was reversed on 2026-05-25 — see §7.0 for the rationale.

**PR 1 — refactor, no UX change.** ✅ Shipped (#6).
- Created `conversations.py` with `ConversationTracker`.
- Migrated `adapter._last_inbound_msg` → `tracker.reply_anchors`
  (rekeyed to `thread_id`).
- Migrated `adapter._resolve_parent_text` → `tracker.get_parent_text`
  with LRU cache.
- Unit tests for LRU bounds, thread_id_of, reply anchors, parent text.

**PR 2 — session sharing in group channels.** ✅ Shipped (#7).
- Computed `thread_id = parent_message_id or message_id` in
  `_process_message`.
- Passed `thread_id` into `SessionSource` for groups (DMs keep
  `thread_id=None` to preserve one-session-per-pair).
- Read `metadata['thread_id']` in `send()`; prefer
  `tracker.get_reply_anchor()` over Hermes core's raw `reply_to`
  (which is `event.message_id` and may itself be a reply — that was
  the bug that caused final responses to leak outside the thread).
- Added `CARBONVOICE_SHARED_GROUP_SESSIONS` env (global override that
  flips `group_sessions_per_user=false` for CV — for bot-room
  channels).
- §7.6 latent bug closed end-to-end.

**PR 3 — v5 transport + `edit_message`.** Next.
- Replace `POST /v3/messages/start` with `POST /v5/messages/text` in
  `api.py`. The new endpoint accepts `thread_id` directly, no
  `reply_to_message_id` mapping required. Removes the `send()`
  reply-anchor lookup that PR 1 introduced (kept for v3 compat only).
- Adopt `idempotency_key` (renamed from `unique_client_id`).
- Add `POST /v5/messages/audio` and `POST /v5/messages/stream` clients
  for follow-up `send_voice` / `edit_message` work.
- Implement `edit_message(chat_id, message_id, content, finalize=)`
  using the streaming endpoint. With this, Hermes core's
  `GatewayStreamConsumer` can render the agent's response as one
  growing bubble instead of N separate messages — the largest visible
  UX win on the roadmap.
- Set `REQUIRES_EDIT_FINALIZE` if CV's stream endpoint distinguishes
  "in-progress" from "finalized" message states (verify).
- Docs + CI smoke tests for the new client methods.

**PR 4 — engaged-thread context via API fetch.** ✅ Shipped.
- `_fetch_thread_context` runs only on the first accepted `@mention`
  in a thread — guarded by `_has_active_session_for_thread` so
  subsequent turns ride on Hermes' SQLite session and don't re-inject
  the parent each prompt.
- CV has no native "list messages in thread" endpoint today, so the
  fetch combines two calls: `GET /messages/<channel_id>/index` for
  ids + `parent_message_id`, then `POST /v5/messages/by-ids` for the
  transcripts. Filters our own bot's prior replies (circular
  context); keeps the thread root even when authored by a bot.
- Result formatted as `[thread parent] name: text` / `name: text`
  lines wrapped in `[Thread context — prior messages …]` /
  `[End of thread context]` delimiters and prepended to the user's
  message text.
- Cached per-thread via `ConversationTracker.set_cached_thread_context`
  (TTL 30 min, LRU 200). Re-mentions in a hot thread reuse the cache;
  long-quiet threads refetch on the next mention.
- Backend ask: a dedicated `GET /v5/messages/<thread_id>/replies`
  (or `getMessageIdsV5` extended with a `thread_id` filter) would
  collapse the workaround to a single call. Tracked in §4.

**PR 5 — local file attachments (Flutter-style flow).** ✅ Shipped.
- `send_document` and `send_image` now accept a URL **or** a local
  file path. URL → single-call `type:"link"` attachment as before.
  Local file → 4-step flow that mirrors the Flutter client:
    1. `POST /v3/attachments/signedurl` → pre-signed S3 PUT URL.
    2. `POST /v5/messages/{text,attachment}` with `type:"file"`,
       canonical link (signed URL minus the query string),
       `status:"Initializing"`.
    3. Return `SendResult(success=True)` to the caller immediately —
       the recipient's UI shows the bubble with a placeholder.
    4. Background `asyncio.create_task` PUTs bytes to S3 and PUTs
       `status:"Uploaded"`/`"Failed"` to
       `/messages/{m}/attachment/{a}`.
- Routing between `/v5/messages/text` and `/v5/messages/attachment`
  depends on caption: text+file goes via `/text` (transcript must be
  non-empty there); file-only goes via `/attachment`.
- `send_voice` unchanged: voice memos still use `/v5/messages/audio`
  (multipart, server-side transcription) since that gives the
  voice-memo UX. Plain audio attachments (`.mp3`/`.wav` files) go
  through `send_document` and arrive as generic file attachments.
- Fixes the latent invalid-enum bug from PR 3: the old code sent
  `type:"image"`/`"document"` which aren't in `AttachmentType`; the
  server silently coerced them to `link`. New code uses `type:"file"`
  for binary uploads and `type:"link"` for hosted URLs — both valid.

**PR 6 — lifecycle hooks refactor.** Pure technical hygiene.
- Move `reactions.ack` call to `on_processing_start(event)`.
- Move `mark_read` call to `on_processing_complete(event, outcome)`.
- Optional: tri-state ack reactions (`acknowledged` → swap to `done` /
  `failed` based on `ProcessingOutcome`).
- Shrink `_process_message` / `_dispatch` accordingly.

**PR 7 — thread memory + reply-to-bot shortcuts.** Deferred,
probably unnecessary after PR 4. Original PR 3 from the pre-revision
plan: `tracker.mark_engaged` / `is_engaged` / `record_outbound`,
`MentionGate.evaluate` gets `is_engaged_thread` / `is_reply_to_bot`
inputs, `CARBONVOICE_STRICT_MENTION` env. Re-evaluate after PR 4
dogfooding — if `@mention`-with-thread-context already covers the
"natural conversation" use case, this PR is dropped.

### 7.11 What to start with on the next session

Begin with **PR 3** from the revised §7.10 above: v5 transport +
`edit_message`. The infrastructure for thread_id is already in place
from PR 2; PR 3 swaps the wire format and unlocks streaming.

Suggested first prompt for a fresh Claude session:

> Read `DEVELOPMENT.md` §7 in full (especially §7.0 for the revision
> context), then implement PR 3 from §7.10: migrate `api.py` from
> `POST /v3/messages/start` to `POST /v5/messages/text` with
> `thread_id` and `idempotency_key`, then implement `edit_message`
> using `POST /v5/messages/stream`. Validate manually that a streamed
> agent response renders as one growing bubble instead of N separate
> messages. Reference: the cv-api v5 controller is at
> `src/message/message.controllerv5.ts`; relevant DTOs at
> `src/message/dto/v5/`.
