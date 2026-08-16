# Author capture pipeline V1 — iOS contract handoff

Capture → refine → flag → review for Author mode, on branch
`feature/streaming-health-author-personalization`. Migration:
`017_author_capture_pipeline.sql`.

This is an additive surface. The existing `/author/projects` and
`/author/documents` REST contract is unchanged and untouched; nothing here reads
or writes those tables.

The one idea the whole surface is built around: **what the author actually said
and what LifeSight refined it into are two different things, and the app can
always ask for either one.** Raw captures are append-only and immutable.
Refinement produces a derivative draft version. Review flags explain a possible
problem; they never silently rewrite the author's words.

## 1. Objects and enums

| Object | Notes |
|--------|--------|
| session | One dictation sitting. Owns captures and everything derived from them. |
| capture | One chunk of raw dictation, exactly as spoken or typed. Immutable. |
| draft version | Refined text derived from a capture range. Immutable once written. |
| flag | An advisory note on one draft version. Never applied automatically. |
| decision | The author resolving a flag. May insert a new draft version. |

Enums (the full closed sets):

| Field | Values |
|-------|--------|
| `session.status` | `active` \| `ended` |
| `capture.source` | `voice` \| `typed` |
| `draft_version.refinement_level` | `light_cleanup` \| `preserve_voice` \| `polish` \| `structural_rewrite` |
| `flag.category` | `typo` \| `grammar` \| `repetition` \| `tangent` \| `unclear` \| `contradiction` \| `structure` \| `other` |
| `flag.status` | `open` \| `accepted` \| `rejected` \| `edited` \| `deferred` |
| `decision.decision` | `accept` \| `reject` \| `edit` \| `defer` |

`refinement_level` defaults to `preserve_voice` — both when the field is omitted
and when it is explicitly `null`.

Refinement levels, in words:

| Level | What the model is allowed to do |
|-------|--------------------------------|
| `light_cleanup` | Transcription artifacts only: mishearings, fillers, false starts, doubled words, sentence punctuation. No rewriting. |
| `preserve_voice` | The default. Light cleanup plus the lightest readability edit. Awkward-but-unmistakably-the-author sentences are left alone and flagged instead. |
| `polish` | Tighter prose — trim redundancy, sharpen structure — still audibly this author. |
| `structural_rewrite` | May reorder and reorganise. Meaning, argument, and voice must survive; significant moves must also be flagged. |

Every level shares one system contract: preserve the author's meaning,
vocabulary, phrasing, tone, and distinctive voice, and never convert the text
into generic polished AI prose. When in doubt the model changes less and raises
a flag.

## 2. Immutability guarantee

Enforced twice, on purpose.

**Application layer.** `shared/author_pipeline/store.py` issues only `INSERT`
and `SELECT` against `author_captures` — there is no `UPDATE author_captures`
and no `DELETE FROM author_captures` anywhere in the codebase. The router
registers no `PATCH`, `PUT`, or `DELETE` handler on any capture path, so those
methods return `405 Method Not Allowed`. Refinement and flag decisions insert
new `author_draft_versions` rows and never write back to a capture.

**Database layer.** Migration 017 installs:

```sql
CREATE TRIGGER author_captures_no_update_delete
    BEFORE UPDATE OR DELETE ON author_captures
    FOR EACH ROW EXECUTE FUNCTION author_captures_reject_mutation();
```

`author_captures_reject_mutation()` raises with SQLSTATE `restrict_violation`,
so any statement that touches an existing capture aborts even if it bypasses
the API.

Consequence, accepted deliberately: the raise also fires for cascaded deletes,
so removing a user or a session that owns captures needs an explicit maintenance
transaction that disables the trigger first. No HTTP route in this PR deletes a
user, a session, or a capture.

Draft versions are append-only too. Accepting or editing a flag never rewrites
the version it was raised against — it inserts the next version with
`derived_from_version_id` pointing back at the source.

## 3. Ownership, auth, and errors

Every route depends on `Depends(get_current_user_id)`. Ownership comes from the
JWT only; a `user_id` in a request body is ignored, never stored.

| Status | When |
|--------|------|
| 401 | Missing or invalid bearer token. |
| 400 | Bad range, empty capture range, unsupported level, `edit` with no `replacement_text`, `accept` on a flag with no appliable suggestion. |
| 404 | Session or flag missing **or owned by another user** — the same response either way, so the API is not an existence oracle. |
| 405 | `PATCH` / `PUT` / `DELETE` on a capture path. Captures cannot be mutated. |
| 409 | Capture appended to an ended session; decision on an already-resolved flag. |
| 422 | Body fails schema validation (unknown `source`, unknown `decision`, negative offsets). |
| 502 | The refinement model returned something unparseable. Nothing is stored. |

Pagination is the house shape: `{items, total, limit, offset}` with
`limit` 1–100 (default 50) and `offset >= 0`.

This pipeline does **not** use the Confirm Gate. Every action here is
reversible and derivative: captures are never destroyed, versions accumulate
rather than overwrite, and a flag decision only ever adds a row. There is no
irreversible or destructive action to gate.

## 4. Sessions

### `POST /author/sessions`

```json
{
  "title": "Chapter three, the storm",
  "conversation_id": null,
  "manuscript_id": null
}
```

All three fields are optional and nullable, and the whole body may be omitted —
`POST /author/sessions` with no body starts an untitled session.
`title` is capped at 200 characters;
blank titles are stored as `null`. `conversation_id` and `manuscript_id` are
soft references (no foreign key) so a session outlives whatever it was linked
to.

```json
{
  "id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
  "conversation_id": null,
  "manuscript_id": null,
  "title": "Chapter three, the storm",
  "status": "active",
  "created_at": "2026-08-16T18:04:11Z",
  "ended_at": null
}
```

### `GET /author/sessions`

`?limit=50&offset=0`, newest first, current user only.

```json
{
  "items": [
    {
      "id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
      "conversation_id": null,
      "manuscript_id": null,
      "title": "Chapter three, the storm",
      "status": "active",
      "created_at": "2026-08-16T18:04:11Z",
      "ended_at": null
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

### `GET /author/sessions/{session_id}`

The one call that answers both questions at once. `captures` is what the author
actually said, in sequence order. `draft_versions` is what LifeSight refined it
into, newest version first. They are separate keys and are never merged.
`open_flags` carries only flags still awaiting a decision, oldest first.

```json
{
  "session": {
    "id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
    "conversation_id": null,
    "manuscript_id": null,
    "title": "Chapter three, the storm",
    "status": "active",
    "created_at": "2026-08-16T18:04:11Z",
    "ended_at": null
  },
  "captures": [
    {
      "id": "b21f4c33-1d5e-4a77-8c90-9e2a1b3c4d55",
      "session_id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
      "sequence": 0,
      "source": "voice",
      "raw_text": "um so the rain hadn't stopped for three days you know",
      "captured_at": "2026-08-16T18:04:19Z",
      "created_at": "2026-08-16T18:04:19Z"
    }
  ],
  "draft_versions": [
    {
      "id": "9c7d5e11-3b2a-4f6d-8e1c-5a4b3c2d1e00",
      "session_id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
      "version": 1,
      "refinement_level": "preserve_voice",
      "content": "So the rain hadn't stopped for three days.",
      "source_capture_from": 0,
      "source_capture_to": 0,
      "derived_from_version_id": null,
      "model_identifier": "claude-sonnet-4-6",
      "created_at": "2026-08-16T18:05:02Z"
    }
  ],
  "open_flags": [
    {
      "id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
      "session_id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
      "draft_version_id": "9c7d5e11-3b2a-4f6d-8e1c-5a4b3c2d1e00",
      "category": "unclear",
      "span_start": 3,
      "span_end": 11,
      "explanation": "It is not clear whose rain this is yet — the scene has no place named.",
      "suggested_change": null,
      "status": "open",
      "created_at": "2026-08-16T18:05:02Z"
    }
  ]
}
```

This response is complete, not paginated. For a very long dictation sitting,
read raw text through the paginated captures endpoint instead.

### `POST /author/sessions/{session_id}/end`

No body. Sets `status` to `ended` and stamps `ended_at`. Idempotent — a second
call keeps the original `ended_at`. An ended session accepts no further
captures (`409`) but can still be refined and reviewed.

```json
{
  "id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
  "conversation_id": null,
  "manuscript_id": null,
  "title": "Chapter three, the storm",
  "status": "ended",
  "created_at": "2026-08-16T18:04:11Z",
  "ended_at": "2026-08-16T18:41:30Z"
}
```

## 5. Captures — the provenance view

### `POST /author/sessions/{session_id}/captures`

```json
{ "source": "voice", "raw_text": "um so the rain hadn't stopped for three days you know" }
```

Both fields are required. `raw_text` must be non-empty after trimming; it is
stored byte-for-byte as sent, disfluencies included. The **server** assigns
`sequence` — the first capture in a session is `0` and each append takes the
next integer, allocated in a single statement so concurrent voice chunks cannot
collide. Clients never send a sequence.

```json
{
  "id": "b21f4c33-1d5e-4a77-8c90-9e2a1b3c4d55",
  "session_id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
  "sequence": 0,
  "source": "voice",
  "raw_text": "um so the rain hadn't stopped for three days you know",
  "captured_at": "2026-08-16T18:04:19Z",
  "created_at": "2026-08-16T18:04:19Z"
}
```

### `GET /author/sessions/{session_id}/captures`

`?limit=50&offset=0`, ordered by `sequence` ascending. This is the "what did I
actually say?" view: raw text only, never the refined text.

```json
{
  "items": [
    {
      "id": "b21f4c33-1d5e-4a77-8c90-9e2a1b3c4d55",
      "session_id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
      "sequence": 0,
      "source": "voice",
      "raw_text": "um so the rain hadn't stopped for three days you know",
      "captured_at": "2026-08-16T18:04:19Z",
      "created_at": "2026-08-16T18:04:19Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

There is deliberately no capture update, delete, or single-capture mutation
route. `PATCH`, `PUT`, and `DELETE` on this path return `405`.

## 6. Refinement

### `POST /author/sessions/{session_id}/refine`

```json
{ "refinement_level": "preserve_voice", "capture_from": 0, "capture_to": 3 }
```

| Field | Notes |
|-------|--------|
| `refinement_level` | Optional/nullable. Omitted or `null` → `preserve_voice`. |
| `capture_from` | Optional, `>= 0`. Omitted → the session's lowest capture sequence. |
| `capture_to` | Optional, `>= 0`. Omitted → the session's highest capture sequence. Inclusive. |

`{}` — or no body at all — means "refine everything in this session with
`preserve_voice`".

The call inserts a new `author_draft_versions` row with `version = max + 1`
(first version is `1`) plus its flags, in one transaction. Captures and every
earlier version are left exactly as they were. `source_capture_from` /
`source_capture_to` record the actual inclusive capture-sequence range the
version derives from, so the app can always walk a refined draft back to the
raw words behind it. `model_identifier` is the model that produced the text.

```json
{
  "draft_version": {
    "id": "9c7d5e11-3b2a-4f6d-8e1c-5a4b3c2d1e00",
    "session_id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
    "version": 1,
    "refinement_level": "preserve_voice",
    "content": "So the rain hadn't stopped for three days.",
    "source_capture_from": 0,
    "source_capture_to": 3,
    "derived_from_version_id": null,
    "model_identifier": "claude-sonnet-4-6",
    "created_at": "2026-08-16T18:05:02Z"
  },
  "flags": [
    {
      "id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
      "session_id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
      "draft_version_id": "9c7d5e11-3b2a-4f6d-8e1c-5a4b3c2d1e00",
      "category": "repetition",
      "span_start": 3,
      "span_end": 11,
      "explanation": "You used a very similar phrase two sentences ago — worth checking if it lands twice on purpose.",
      "suggested_change": "the downpour",
      "status": "open",
      "created_at": "2026-08-16T18:05:02Z"
    }
  ]
}
```

Error cases: `404` if the session is missing or not yours (checked before the
model is called, so a blocked refine costs nothing); `400` if the session has no
captures, if `capture_from > capture_to`, or if the range selects no captures;
`502` if the model reply cannot be parsed into refined content — in which case
no version and no flags are written.

## 7. Flags

A flag is an explanation, not an edit. It always belongs to exactly one draft
version (`draft_version_id`) and is scoped to that version's text.

| Field | Notes |
|-------|--------|
| `category` | One of the eight category strings. Anything the model returns outside that set is recorded as `other` rather than dropped. |
| `span_start` / `span_end` | Character offsets into that draft version's `content`, start inclusive and end exclusive. Both are set, or **both are `null`**. |
| `explanation` | Always present and non-empty. Written as one spoken sentence — it is read aloud. |
| `suggested_change` | Nullable. Present only on a localized flag, and only when there is a concrete replacement for exactly that span. |
| `status` | `open` on creation; set by a decision. |

A flag with `span_start: null` is **advisory only**: there is nowhere to apply a
change, so it can be rejected or deferred but not accepted or edited. The way to
act on an advisory flag is to refine again.

Flags are never migrated between versions. When a decision creates a new
version, the other flags on the older version stay open against that older
version and keep their original offsets — display each flag against the version
it names.

## 8. Review decisions

### `POST /author/flags/{flag_id}/decision`

```json
{ "decision": "edit", "replacement_text": "the downpour" }
```

| Decision | Effect on the flag | Effect on the text |
|----------|--------------------|--------------------|
| `accept` | `accepted` | Inserts a new draft version with the flag's `suggested_change` substituted over its span. |
| `edit` | `edited` | Inserts a new draft version with `replacement_text` substituted over the flag's span. |
| `reject` | `rejected` | None. No new version. |
| `defer` | `deferred` | None. No new version. |

`replacement_text` is required for `edit` (`400` without it) and ignored for
`reject` and `defer`. `accept` needs the flag to carry a `suggested_change`
(`400` without one). Both `accept` and `edit` need the flag to be localized
(`400` on an advisory flag). A flag can be decided once — a second decision
returns `409`.

The new version derives from the flagged version, not from the newest one:
`derived_from_version_id` is the flagged version's id, `refinement_level` and
the capture range are inherited from it, and `model_identifier` is `null`
because the change came from the author, not a model. No capture is read or
written by any decision.

```json
{
  "flag": {
    "id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    "session_id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
    "draft_version_id": "9c7d5e11-3b2a-4f6d-8e1c-5a4b3c2d1e00",
    "category": "repetition",
    "span_start": 3,
    "span_end": 11,
    "explanation": "You used a very similar phrase two sentences ago — worth checking if it lands twice on purpose.",
    "suggested_change": "the downpour",
    "status": "edited",
    "created_at": "2026-08-16T18:05:02Z"
  },
  "decision": {
    "id": "4d5e6f70-8a9b-4c1d-2e3f-4a5b6c7d8e9f",
    "flag_id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    "decision": "edit",
    "replacement_text": "the downpour",
    "resulting_draft_version_id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
    "decided_at": "2026-08-16T18:07:44Z"
  },
  "draft_version": {
    "id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
    "session_id": "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01",
    "version": 2,
    "refinement_level": "preserve_voice",
    "content": "So the downpour hadn't stopped for three days.",
    "source_capture_from": 0,
    "source_capture_to": 3,
    "derived_from_version_id": "9c7d5e11-3b2a-4f6d-8e1c-5a4b3c2d1e00",
    "model_identifier": null,
    "created_at": "2026-08-16T18:07:44Z"
  }
}
```

For `reject` and `defer` the shape is identical except `draft_version` is `null`
and `decision.resulting_draft_version_id` is `null`.

## 9. Typical iOS flow

1. `POST /author/sessions` when the author starts dictating.
2. `POST /author/sessions/{id}/captures` per chunk, as it arrives. Never
   batched into an edit — every chunk is its own permanent row.
3. `POST /author/sessions/{id}/refine` when the author asks to hear it back
   cleaned up. Read `draft_version.content` aloud.
4. Read each `open_flags[].explanation` aloud and collect a spoken decision, one
   flag at a time, via `POST /author/flags/{id}/decision`.
5. `GET /author/sessions/{id}` any time to re-read either side —
   `captures` for "what I actually said", `draft_versions[0]` for the current
   refined draft.
6. `POST /author/sessions/{id}/end` when the sitting is over. Review still
   works afterwards.

## 10. Not in this PR

- No chat-tool wiring: Author `/chat` does not yet call this pipeline, so there
  are no new `pending_action`, `visual_panel`, or `client_actions` shapes.
- No link from a draft version into `/author/documents` — `manuscript_id` is
  stored as a soft reference and nothing consumes it yet.
- No capture, session, or version deletion route, and no export endpoint.
- No streaming refinement; `POST /refine` is a single request/response.
