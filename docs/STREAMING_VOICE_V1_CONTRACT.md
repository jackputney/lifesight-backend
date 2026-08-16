# Streaming voice V1 — iOS contract handoff (`WS /chat/stream`)

Additive transport on branch `feature/streaming-health-author-personalization`.
It streams the same chat turn REST `POST /chat` produces, plus streamed speech
and barge-in. **REST `POST /chat` and `POST /voice/speech` are unchanged** — a
client that never opens this socket keeps working exactly as before.

Nothing here weakens the Confirm Gate: no irreversible action ever executes
over this socket (§9).

## 1. Connecting

| Item | Value |
|------|-------|
| URL | `wss://<host>/chat/stream` (`ws://` for local dev) |
| Auth | `Authorization: Bearer <access JWT>` — **handshake header** |
| Subprotocol | none |
| Message encoding | UTF-8 JSON text frames, one JSON object per frame |
| Max `message` length | 16000 characters |
| Max whole-frame length | 64000 bytes — a larger frame is rejected unparsed with `invalid_frame` |
| Max concurrent sockets per user | 4 — see §1.2 |

The token goes in the HTTP upgrade request's headers. On iOS that means setting
it on the `URLRequest` before creating the task:

```swift
var request = URLRequest(url: streamURL)
request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
let task = URLSession.shared.webSocketTask(with: request)
```

**Never put the token in the URL or a query parameter.** Query strings are
logged by proxies and are not covered by TLS at the CDN boundary.

### 1.1 Rejected handshakes

A missing, malformed, or expired token fails the handshake with **HTTP 401**
before the upgrade completes. There is no websocket connection and no `error`
frame — `URLSessionWebSocketTask` surfaces this as a connection failure with a
401 response. Refresh the session and reconnect; do not retry with the same
token. (`AUTH_MODE=dev` on a local backend accepts any/no token and resolves to
the fixed dev user, matching every other route.)

### 1.2 Connection limit and close codes

Each user may hold **at most 4** concurrent `/chat/stream` sockets. Every socket
carries its own model stream and TTS session, so the cap is a resource guard,
not a product rule — one socket per foreground app is the intended usage.

A connection over the cap is accepted and then immediately closed; it never
receives an `error` frame.

| Close code | Meaning | Client action |
|------------|---------|---------------|
| `1000` | Normal close (either side) | Nothing |
| `4029` | Too many concurrent connections for this user | Close any sockets you are no longer using and reconnect once; do not reconnect in a loop |

Any other abnormal close (e.g. `1006`) is a transport failure: reconnect and
resume per §8.

## 2. Client → server frames

### 2.1 `user_turn`

```json
{
  "type": "user_turn",
  "mode": "fitness",
  "message": "How many sets do I have left?",
  "conversation_id": null,
  "voice": { "enabled": true }
}
```

| Field | Notes |
|-------|--------|
| type | `"user_turn"` |
| mode | Same strings as REST `/chat`: `fitness` \| `diet` \| `author` \| `brainstorm` \| `mail_calendar` \| `checkin`. Unknown/retired (`health`) → `error` with code `unsupported_mode`. |
| message | Required, 1–16000 chars. The user's transcribed speech. |
| conversation_id | `null` to start a new conversation, otherwise the UUID echoed back by `turn_started`. Stored mode wins on resume, exactly like REST. |
| voice | Optional. `{ "enabled": false }` suppresses all `audio_chunk` frames for the turn. Omitted → voice **on**. |

Sending `user_turn` while a turn is still running is an implicit barge-in: the
running turn is cancelled first (§5) and the new one starts.

### 2.2 `interrupt`

```json
{ "type": "interrupt", "turn_id": "9f0c7c9c-6f9a-4b2f-9d61-1a1a2b3c4d5e" }
```

| Field | Notes |
|-------|--------|
| type | `"interrupt"` |
| turn_id | The `turn_id` from `turn_started`. A stale or unknown id is ignored silently — it never cancels a different turn. |

## 3. Server → client frames

Every frame has `type`. Every frame except a connection-level `error` carries
`turn_id`.

### 3.1 `turn_started`

**No frame ever carries a `turn_id` the client has not seen in a
`turn_started` first.** `turn_started` is the first frame of every turn, and a
turn that fails before it could be sent — an unsupported `mode`, a
`conversation_id` that isn't a UUID, a conversation belonging to another user —
is reported as a connection-level `error` with `turn_id: null` instead. Allocate
per-turn state on `turn_started` and treat a `turn_id: null` error as "the turn
never started", with nothing to clean up.

```json
{
  "type": "turn_started",
  "turn_id": "9f0c7c9c-6f9a-4b2f-9d61-1a1a2b3c4d5e",
  "conversation_id": "3c2f0a2e-9b41-4b0a-9a1f-7f2c4e1d8b30"
}
```

`conversation_id` is server-generated when the request sent `null`. Echo it on
every later `user_turn` in the same conversation.

### 3.2 `text_delta`

```json
{
  "type": "text_delta",
  "turn_id": "9f0c7c9c-6f9a-4b2f-9d61-1a1a2b3c4d5e",
  "delta": "You have two sets left "
}
```

Deltas arrive in order and are already-final text — never revise earlier text.
Append them to the on-screen transcript as they arrive.

### 3.3 `audio_chunk`

```json
{
  "type": "audio_chunk",
  "turn_id": "9f0c7c9c-6f9a-4b2f-9d61-1a1a2b3c4d5e",
  "sequence": 0,
  "kind": "assistant",
  "mime_type": "audio/mpeg",
  "data_base64": "SUQzBAAAAAA..."
}
```

| Field | Notes |
|-------|--------|
| sequence | Per-turn, starts at `0`, strictly increasing by 1 with no gaps. Play in `sequence` order. |
| kind | `assistant` \| `stall` — see §6 and §7. |
| mime_type | Always `"audio/mpeg"` in V1. |
| data_base64 | Standard base64 of raw MP3 bytes. |

### 3.4 `turn_cancelled`

```json
{ "type": "turn_cancelled", "turn_id": "9f0c7c9c-6f9a-4b2f-9d61-1a1a2b3c4d5e" }
```

Sent **exactly once** per cancelled turn, and it is the last frame for that
turn. There is no `response_complete` for a cancelled turn.

### 3.5 `error`

```json
{
  "type": "error",
  "turn_id": "9f0c7c9c-6f9a-4b2f-9d61-1a1a2b3c4d5e",
  "code": "tts_unavailable",
  "message": "ELEVENLABS_API_KEY is not configured"
}
```

`turn_id` is `null` for connection-level problems — a malformed or oversized
frame, an unsupported mode, and any conversation-resolution failure, because
those all happen before `turn_started` (§3.1). `message` is a short
human-readable string safe to log; it never contains user speech or model
output. See §8 for every `code`.

### 3.6 `response_complete`

Terminal frame of a successful turn. Same field semantics as the REST
`ChatResponse` body.

```json
{
  "type": "response_complete",
  "turn_id": "9f0c7c9c-6f9a-4b2f-9d61-1a1a2b3c4d5e",
  "conversation_id": "3c2f0a2e-9b41-4b0a-9a1f-7f2c4e1d8b30",
  "reply": "You have two sets left on bench press.",
  "pending_action": null,
  "visual_panel": {
    "type": "exercise",
    "data": {
      "exercise_id": null,
      "exercise_name": "Bench Press",
      "sets": 4,
      "reps": 8,
      "rest_seconds": 90,
      "current_set": 3,
      "notes": null
    }
  },
  "research": null,
  "client_actions": []
}
```

The `exercise` panel's `data` is always all seven fields, exactly as REST
`/chat` sends it. `exercise_id`, `current_set` and `notes` are nullable;
`exercise_name`, `sets`, `reps` and `rest_seconds` are always present. Decode
other `type` values leniently — `data` is an open object per panel type.

| Field | Notes |
|-------|--------|
| reply | Full assistant text for the turn. For an ordinary single-round turn this is exactly the concatenation of the turn's `text_delta` values, trimmed. When the model speaks before calling a tool, each round's text is trimmed and joined with a blank line. Treat `reply` as authoritative for what to store in history. |
| pending_action | `null` or `{ "action_id": "...", "description": "..." }`. `description` is read aloud. |
| visual_panel | `null` or `{ "type": "...", "data": { ... } }`. Same panels as REST. |
| research | `null` or the Brainstorm research object from `10-api-contract.mdc`. |
| client_actions | Always an array, `[]` on ordinary turns. Same `navigate` / `open_conversation` / `refresh_profile` items as REST. |

## 4. Ordering and sequencing guarantees

1. `turn_started` is the first frame of a turn; `response_complete` or
   `turn_cancelled` is the last. A turn that fails before `turn_started` emits
   no frame carrying its `turn_id` — the failure arrives as an `error` with
   `turn_id: null` (§3.1), so an unknown `turn_id` never reaches the client.
2. Exactly one turn is active at a time. Once a turn is superseded or
   cancelled, the server emits nothing further for it — a late chunk from an
   abandoned generation can never appear after a newer turn's `turn_started`.
3. `text_delta` frames for a turn arrive in generation order.
4. `audio_chunk.sequence` is per-turn, starts at `0`, and increases by exactly
   1 across both `assistant` and `stall` chunks.
5. All `audio_chunk` frames for a turn precede its `response_complete`.
6. An `error` frame does not by itself end a turn. A recoverable error (e.g.
   `tts_unavailable`) is followed by a normal `response_complete`; a fatal one
   (`model_error`, `internal_error`, `unsupported_mode`) is not.

## 5. Interrupt / barge-in

The user talking over the assistant is the normal case, not an edge case.

**Client:**

1. Stop local playback immediately and discard buffered audio — do not wait for
   the server.
2. Send `{"type":"interrupt","turn_id":"<current turn>"}`.
3. Expect exactly one `turn_cancelled` for that `turn_id`, and no further
   frames for it. Frames already in flight when the interrupt was sent may
   still arrive before `turn_cancelled` — drop any frame whose `turn_id` is a
   turn you have cancelled.
4. Send the next `user_turn` **with the same `conversation_id`**.

**Server:** the Claude stream is closed, the ElevenLabs socket for that turn is
closed (that API has no cancel message — closing the socket *is* the cancel),
queued audio stops, and one `turn_cancelled` is sent.

Sending a `user_turn` without an `interrupt` first has the same effect: the
in-flight turn is cancelled (you will still receive its `turn_cancelled`) and
the new turn starts.

### 5.1 What the interruption does to the conversation

- The `conversation_id` **does not change**. An interruption never forks a new
  conversation; keep using the id from the original `turn_started`.
- Resumption is entirely driven by what the client sends. A follow-up
  `user_turn` carrying the same `conversation_id` continues the interrupted
  conversation, interrupted partial and all. A follow-up with
  `conversation_id: null` starts a **brand-new** conversation with its own id —
  the interrupted history stays where it was and is not carried over. Always
  echo the id after a barge-in unless the user actually wanted a fresh start.
- The user's message is persisted as soon as the turn reaches the model, so the
  next turn has full context. Interrupting within the first few milliseconds —
  before generation starts — can cancel the turn before that write, in which
  case nothing at all was recorded and the utterance can simply be re-sent.
- Whatever assistant text was actually produced before the interrupt is
  persisted, with a trailing line
  `[Interrupted by the user before this reply finished.]`, so history never
  reads a half-written reply as a complete one. This line is visible in
  `GET /conversations/{id}/messages`.
- A tool round interrupted before it finished is discarded entirely from
  history — the transcript never contains a tool call without its result.
- If a tool had already staged a `pending_action` before the interrupt, that
  action is never surfaced, expires unused after 10 minutes, and executes
  nothing.

## 6. Audio encoding and playback

- Output format is `mp3_44100_128` (MPEG audio, 44.1 kHz, 128 kbps CBR), base64
  encoded in `data_base64`.
- `kind: "assistant"` chunks are **fragments of one continuous MP3 stream for
  the turn**. Decode base64 and append the bytes, in `sequence` order, to a
  single streaming decoder (`AVAudioEngine` with an MP3 converter, or
  `AVSampleBufferAudioRenderer`). Do not treat each chunk as a standalone file.
- `kind: "stall"` chunks are **complete standalone MP3 files** — one frame is
  one whole clip.
- Concatenating all `assistant` chunks of a turn in `sequence` order yields the
  full spoken reply even when a `stall` chunk is interleaved between them.
- With `voice.enabled = false` no `audio_chunk` frames are sent at all.

## 7. Stall audio (`kind: "stall"`)

When a turn calls a tool that takes real time, the backend may play a short
cached clip so the user isn't left in silence. These are always truthful: the
clip describes the work that is actually starting, chosen from a closed
server-side allowlist. There is no "almost done" style fake-progress phrase,
and user or private text is never synthesized.

| Situation | Spoken | Reachable in V1 |
|-----------|--------|-----------------|
| Calendar lookup | "Let me check your calendar." | Yes (`list_calendar_events`) |
| Health data lookup | "I'm checking your latest health data." | Yes (`get_recent_health_data`) |
| Mail lookup | "Let me look at your mail." | **No** — the mapping exists for the mail tools of a later slice; no mode currently exposes a mail tool, so this clip is never emitted. Don't build UI for it yet. |
| Any other tool | "One moment while I pull that up." | Yes (fallback) |

A stall clip is emitted at most once per phrase per turn, and only when the
model didn't already say what it was doing. Stall audio is interruptible like
any other audio: on `interrupt`, stop it immediately.

## 8. Error codes and recovery

| code | turn_id | Meaning | Client action |
|------|---------|---------|---------------|
| `invalid_frame` | `null` | Frame wasn't valid JSON, was over 64000 bytes, or didn't match a known shape | Fix the frame; the socket stays open |
| `unsupported_mode` | `null` | `mode` isn't a valid chat mode | Correct the mode; the socket stays open |
| `invalid_conversation` | `null` | `conversation_id` isn't a UUID | Retry with `null` to start fresh; the socket stays open |
| `forbidden_conversation` | `null` | Conversation belongs to another user | Do not retry; start a new conversation |
| `model_unavailable` | set | Backend has no model credentials configured | Surface a spoken "I can't reach my brain right now"; retry later |
| `model_error` | set | Model produced nothing usable | Offer to retry the turn |
| `tts_unavailable` | set | Voice couldn't start (provider unconfigured/unreachable) | **Non-fatal.** Text continues; fall back to on-device speech if desired |
| `tts_error` | set | Voice failed mid-turn | **Non-fatal.** Keep whatever audio played; text continues |
| `internal_error` | set | Unexpected server failure | Offer to retry the turn |

`tts_unavailable` and `tts_error` are the only non-fatal codes, and they are
only ever sent after `turn_started` — a `response_complete` still follows.
Every other code with a `turn_id` set ends that turn without a
`response_complete`. Codes with `turn_id: null` refer to no turn at all: the
socket stays usable and nothing needs to be cleaned up client-side.

If the socket drops, reconnect and resume by sending the next `user_turn` with
the same `conversation_id`. There is no replay of missed frames; the durable
record is `GET /conversations/{id}/messages`.

## 9. Confirm Gate — unchanged over the socket

A streamed turn **never executes an irreversible action**. Tools may only stage
a `pending_action`, which surfaces in `response_complete` exactly as it does
over REST. Executing it still requires `POST /confirm` with
`{"action_id": "...", "approved": true}`. There is no confirm/execute frame on
this socket, and none will be added — the Confirm Gate stays a separate,
explicit HTTP round trip.

## 10. Compatibility

- `POST /chat` is preserved unchanged and remains the supported non-streaming
  path. Clients may use either; they share conversations, history, modes,
  prompts, tools and the Confirm Gate.
- `POST /voice/speech` (one-shot `audio/mpeg` TTS) is unchanged and is still
  the right call for short spoken UI strings outside a chat turn.
- `GET /modes` is unaffected: `/chat/stream` is a transport, not a mode.

## 11. Open cross-repo question — MP3 vs PCM

V1 streams MP3 (`mp3_44100_128`) because it matches `POST /voice/speech` and
keeps frames small. ElevenLabs can also emit raw PCM
(`pcm_16000` / `pcm_22050` / `pcm_24000`), which would let iOS schedule audio
sample-accurately and cut playback dead exactly at the barge-in point, at the
cost of roughly 5–10× the bytes on the wire and no container framing.

**This is not decided.** If the iOS playback path wants sample-accurate
barge-in, say so and we'll add an output-format negotiation field to
`user_turn` rather than switching unilaterally — the change affects both repos.

## 12. Server configuration (backend only)

| Env | Purpose |
|-----|---------|
| `ELEVENLABS_API_KEY` | Required for any audio. Unset → `tts_unavailable`. |
| `ELEVENLABS_VOICE_ID` | Required for any audio. |
| `ELEVENLABS_MODEL_ID` | Optional, defaults to `eleven_flash_v2_5`. |
| `STALL_AUDIO_CACHE_DIR` | Optional. On-disk cache root for stall clips; defaults to a temp directory. |

Keys stay server-side. iOS never holds an ElevenLabs or Anthropic credential.

## 13. Known V1 limits

- One ElevenLabs streaming session per assistant turn (not per sentence).
  Multi-context stream-input is a future optimization, deliberately skipped in
  V1 because of its 5-context limit and phantom-context behaviour.
- Research (`research`) is still produced non-streamed: Brainstorm research
  turns deliver their text in one `text_delta` before `response_complete`.
- No frame replay after a dropped socket.
