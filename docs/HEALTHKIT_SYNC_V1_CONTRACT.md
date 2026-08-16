# HealthKit sync V1 — iOS contract handoff

Additive backend contract on branch `feature/streaming-health-author-personalization`.
Existing `/chat` fields are unchanged; nothing here adds a response field to
`/chat`.

Migration: `016_health_samples.sql` (tables `health_samples`, `health_sync_state`;
deprecates `health_metrics` in place).

New endpoints: `POST /healthkit/sync`, `GET /healthkit/status`.
New chat tool (Fitness + Diet only): `get_recent_health_data`.

## 1. Sample vocabulary (closed)

`type` is a closed allowlist. Anything else is ignored at ingest — the server
never invents a new type.

`steps` | `heart_rate` | `resting_heart_rate` | `sleep` | `workout` |
`active_energy` | `distance_walking_running` | `body_mass`

Client units are converted to one canonical unit per type at ingest, so stored
values are always comparable. A numeric sample whose `unit` is missing or not
in the accepted list for its type is ignored.

| Field | Notes |
|-------|--------|
| steps | canonical `count`; accepted `count`, `steps` |
| heart_rate | canonical `count/min`; accepted `count/min`, `count/minute`, `bpm` |
| resting_heart_rate | canonical `count/min`; accepted `count/min`, `count/minute`, `bpm` |
| active_energy | canonical `kcal`; accepted `kcal`, `cal`, `kj` |
| distance_walking_running | canonical `m`; accepted `m`, `km`, `mi`, `ft` |
| body_mass | canonical `kg`; accepted `kg`, `g`, `lb` |
| sleep | canonical `min`; accepted `min`, `minute`, `s`, `sec`, `hr`, `h`. Categorical: may instead send `value_text` (e.g. a sleep stage) with `value` and `unit` omitted |
| workout | canonical `min`; accepted `min`, `minute`, `s`, `sec`, `hr`, `h`. Categorical: may instead send `value_text` (e.g. the activity name) with `value` and `unit` omitted |

Only `sleep` and `workout` may be stored without a unit, and only when they
carry `value_text`. Every sample must have at least one of `value` or
`value_text`.

## 2. `POST /healthkit/sync`

Requires `Authorization: Bearer <access JWT>`. The body never carries a
`user_id` — ownership is the token's user.

At most **1000 samples per request** (`MAX_SYNC_BATCH`). The limit is enforced on
the `samples` field itself, so an oversized batch is rejected as a **422** as
soon as the server passes the cap, without walking the rest of the list — split
larger uploads client-side and send them sequentially. Nothing from a rejected
batch is written.

Request:

```json
{
  "samples": [
    {
      "sample_id": "F4D2C1A0-3B77-4E21-9E0C-2A9B7C10D5E3",
      "type": "heart_rate",
      "start_at": "2026-08-15T14:03:00Z",
      "end_at": "2026-08-15T14:03:00Z",
      "value": 62,
      "unit": "count/min",
      "source_bundle": "com.apple.health",
      "source_name": "Apple Watch"
    },
    {
      "sample_id": "9C55B2E7-1A44-4E90-B1F2-70D4C3E80A11",
      "type": "sleep",
      "start_at": "2026-08-15T05:10:00Z",
      "end_at": "2026-08-15T06:35:00Z",
      "value": 85,
      "unit": "min",
      "value_text": "asleep_core",
      "source_bundle": "com.apple.health",
      "source_name": "Apple Watch"
    }
  ]
}
```

| Field | Notes |
|-------|--------|
| samples | required array, **1000 items max**. Over that the whole request is a 422 |
| sample_id | required string, 1–200 chars. The HealthKit sample UUID. This is the dedupe key. `""` is a 422; a whitespace-only string is per-sample `ignored` |
| type | required string, 1–64 chars, and one of the closed allowlist above. Over 64 chars is a 422; any other unrecognized value is per-sample `ignored` |
| start_at | required string, 1–64 chars, ISO 8601; `Z` accepted. Naive timestamps are read as UTC |
| end_at | required string, 1–64 chars, ISO 8601, must be `>= start_at` |
| value | optional number, `null` allowed. Required unless the type is categorical and `value_text` is set. A numeric string is coerced; a non-numeric string is a 422; a non-finite number (`NaN` / `Infinity`) is per-sample `ignored` |
| unit | optional string, `<=32` chars. Required whenever `value` is set |
| value_text | optional string, `<=120` chars. Stored for any type when sent; it is the only accepted payload for `sleep` / `workout` sent without a `value` |
| source_bundle | optional string, `<=200` chars. Trimmed; empty or whitespace-only is stored as `null` |
| source_name | optional string, `<=200` chars. Trimmed; empty or whitespace-only is stored as `null` |

No other keys are read. Unknown extra keys in a sample object are ignored, not
rejected.

Response:

```json
{
  "accepted": 2,
  "updated": 0,
  "ignored": 0,
  "server_time": "2026-08-15T14:05:12.481239Z"
}
```

| Field | Notes |
|-------|--------|
| accepted | integer — rows newly inserted this request |
| updated | integer — rows that already existed under this `sample_id` and whose stored content changed |
| ignored | integer — everything else: samples rejected by per-sample validation, duplicate `sample_id`s inside the same batch, and re-sent samples identical to what is already stored |
| server_time | string, never null — when this sync completed, server clock. Same value written to `last_synced_at` |

All four fields are always present and never `null`.

Every timestamp this API returns (`server_time`, `last_synced_at`,
`latest_sample_at`) is UTC ISO 8601 with a literal `Z` suffix and **fractional
seconds up to 6 digits**, omitted only when they are exactly zero — decode with
a formatter that tolerates both (e.g. `.withFractionalSeconds` with a plain
ISO-8601 fallback), not a fixed-millisecond one.

`accepted + updated + ignored` always equals the number of samples in the
request.

### Dedupe and idempotency

Rows are upserted on `(user_id, 'healthkit', sample_id)`.

- Re-sending an identical sample changes nothing and counts as `ignored`, so a
  device may safely re-upload an overlapping window.
- Re-sending the same `sample_id` with a changed `value`, `unit`, `value_text`,
  interval, or source updates the existing row in place — it never creates a
  second row — and counts as `updated`.
- If one batch contains the same `sample_id` twice, the **last** occurrence
  wins and the earlier ones count as `ignored`.
- The same `sample_id` from two different users is two independent rows.

### Validation

Validation happens in two layers, and they fail differently. Read both before
building the upload path.

**Per-sample (`ignored`) — one malformed sample never fails the batch.** Each of
these is counted in `ignored` and the rest of the batch still lands:

- `type` not in the allowlist
- `unit` missing on a numeric sample, or not accepted for that `type`
- `end_at` earlier than `start_at`
- `start_at` or `end_at` not parseable as ISO 8601
- neither `value` nor `value_text` present, or `value` not a finite number
  (`NaN` / `Infinity`)
- `sample_id` that is empty after trimming (e.g. `"   "`)

**Whole-batch (422) — nothing is written and every sample is rejected**, even
if only one is at fault:

- more than 1000 samples in the request
- a required field missing or the wrong JSON type (`samples` absent, `type`
  absent, `value` a non-numeric string, …)
- `sample_id` sent as `""`
- **any string field over its length limit**: `sample_id` > 200, `type` > 64,
  `start_at` / `end_at` > 64, `unit` > 32, `value_text` > 120,
  `source_bundle` / `source_name` > 200

That last one is the trap: an over-long `unit` or `source_name` is the same
*class* of problem as an unrecognized `unit` (bad field content), but it fails
the entire upload with a 422 instead of being counted in `ignored`. **The client
must truncate `unit`, `value_text`, `source_bundle`, `source_name`, `type` and
`sample_id` to the limits above before sending** — HealthKit source names and
categorical labels are device-supplied and can be arbitrarily long. Truncating
loses nothing: `sample_id`, `value_text`, `source_bundle` and `source_name` are
trimmed and clipped to exactly these lengths server-side anyway once they pass.

A 422 identifies the offending field in `detail[].loc`, e.g.
`["body", "samples", 7, "unit"]` for the eighth sample's `unit`. The rejected
payload is **not** echoed back in the error.

### Errors

| Status | When |
|--------|--------|
| 401 | missing, malformed, or expired bearer token. `{"detail": "Missing bearer token"}` or `{"detail": "Invalid or expired token"}` |
| 422 | any whole-batch failure from the list above — including **more than 1000 samples**. Nothing is written; split the batch |
| 503 | database temporarily unavailable: `{"detail": "Database temporarily unavailable"}` (backend-wide handler) |

> **Changed from the first draft of this contract:** an oversized batch now
> returns **422, not 400**. The cap moved onto the `samples` field so a huge
> body is rejected before the server allocates one object per sample (a 72 MB
> body previously drove peak RSS past 800 MB before answering 400, enough to
> OOM the container). A client that special-cases 400 for "batch too large"
> must be updated to treat 422 with `detail[0].type == "too_long"` the same
> way. No other status code changed.

The 422 body is FastAPI's standard validation shape, minus the echoed input:

```json
{
  "detail": [
    {
      "type": "too_long",
      "loc": ["body", "samples"],
      "msg": "List should have at most 1000 items after validation, not 1001",
      "ctx": { "field_type": "List", "max_length": 1000, "actual_length": 1001 }
    }
  ]
}
```

`ctx` is present only for errors that carry one. The rejected samples are never
included, so a 422 response stays small no matter how large the request was.

## 3. `GET /healthkit/status`

Requires the same bearer token. Returns counts and timestamps only — it never
returns a sample, a `sample_id`, or any other user's data.

```json
{
  "last_synced_at": "2026-08-15T14:05:12.481239Z",
  "categories": {
    "steps": { "latest_sample_at": "2026-08-15T13:58:00Z", "count_last_30d": 412 },
    "heart_rate": { "latest_sample_at": "2026-08-15T14:03:00Z", "count_last_30d": 8841 },
    "resting_heart_rate": { "latest_sample_at": "2026-08-15T06:40:00Z", "count_last_30d": 29 },
    "sleep": { "latest_sample_at": "2026-08-15T06:35:00Z", "count_last_30d": 143 },
    "workout": { "latest_sample_at": "2026-08-14T18:12:00Z", "count_last_30d": 11 },
    "active_energy": { "latest_sample_at": "2026-08-15T13:58:00Z", "count_last_30d": 2210 },
    "distance_walking_running": { "latest_sample_at": "2026-08-15T13:58:00Z", "count_last_30d": 388 },
    "body_mass": { "latest_sample_at": "2026-08-12T07:02:00Z", "count_last_30d": 4 }
  }
}
```

| Field | Notes |
|-------|--------|
| last_synced_at | ISO 8601 string or `null` — when `POST /healthkit/sync` last completed for this user. Read from `health_sync_state`, not derived from samples, so a sync that uploaded only known samples still counts. `null` until the first sync |
| categories | object, always present — contains **all eight** allowlisted types, in the allowlist order, so the client decodes a fixed shape. Both keys inside each entry are always present |
| categories[type].latest_sample_at | ISO 8601 string or `null` — `MAX(start_at)` over all of this user's samples of that type, any provider, with no time window. Echoes the precision the device sent, so it may have no fractional seconds |
| categories[type].count_last_30d | integer, never null, `0` when there is nothing. Counts this user's samples of that type, any provider, whose `start_at` is within the last 30 days |

Terra-sourced samples are included in these aggregates: the status endpoint
answers "what health data does the server hold for me", not "what did the
phone upload".

## 4. Ownership

- Every read and write is filtered by the `user_id` resolved from the JWT via
  `Depends(get_current_user_id)`. There is no route parameter or body field
  that names another user.
- Cross-user access is not possible to observe: a user with no data gets an
  empty status (`last_synced_at: null`, all counts `0`), not a 403, so the API
  is not an existence oracle for other accounts.
- `health_samples.user_id` is `REFERENCES users(id) ON DELETE CASCADE` — deleting
  a user removes their samples and sync state.
- Health values are never written to logs.
- The 401s above are what a deployed backend returns (`AUTH_MODE=self`, which
  staging and production require). A local backend left on the default
  `AUTH_MODE=dev` resolves every request — including one with no token — to a
  fixed dev user, so a client cannot infer auth correctness from a local run.

## 5. Terra reconciliation — why `health_metrics` is deprecated

`health_samples` is one normalized table for **both** providers, discriminated
by `provider` (`healthkit` | `terra`). There is no HealthKit-only parallel
store.

The pre-existing `health_metrics` (migration `003`) could not serve this:

- one `recorded_at` instead of a `start_at` / `end_at` interval
- no external sample id and no unique constraint, so every webhook replay or
  device re-sync duplicated rows
- no `unit` column, so values of one metric were not comparable
- no `provider` column
- free-text `metric_type`, which makes per-type aggregates meaningless

Nothing in the codebase ever read `health_metrics`, so migrating the writer is
not a breaking change. `POST /wearables/terra/webhook` now normalizes into
`health_samples` with `provider: "terra"` and no longer writes
`health_metrics`. The old table is left in place — no `DROP`, no data loss — and
carries a `COMMENT ON TABLE` marking it deprecated and superseded.

Terra payloads have no stable per-sample id, so ingest synthesizes a
deterministic one: `sha256(metric_type|recorded_at|source_device|value)`
truncated to 40 hex characters. A replayed webhook therefore upserts the same
rows instead of duplicating them. Terra summary metrics are point-in-time, so
`start_at == end_at == recorded_at`.

Because `recorded_at` is part of that hash, the idempotency guarantee only
holds for metrics Terra actually timestamps. A metric with a missing or
unparseable `recorded_at` is therefore **dropped and counted in `ignored`**, not
stamped with the server clock: substituting `now()` would hash a different id on
every delivery — reintroducing exactly the duplicate rows this design removes —
and would also record a health reading at a time it was not taken.

A Terra metric is dropped and counted in `ignored` when it has:

- no entry in the metric mapping (a new Terra field never invents a `sample_type`)
- no numeric `value` (e.g. the raw nested-object rows)
- no parseable `recorded_at`
- a duplicate synthesized id inside the same payload

The webhook response keeps its shape and adds one counter:

```json
{ "ok": true, "written": 6, "ignored": 3 }
```

`written` is inserted + updated rows; `ignored` is the dropped metrics above
plus replayed rows that changed nothing — so replaying a delivery returns
`written: 0`. A payload with no resolvable user acks as
`{"ok": true, "written": 0, "ignored": 0}`, and a bad signature is a 401.
`POST /wearables/connect` is unchanged.

## 6. Chat tool `get_recent_health_data` — bounded context

Available in `fitness` and `diet` modes only. It is **pull-only**: no health
data is ever injected into a system prompt, and nothing runs unless Claude
calls the tool during a turn.

Input:

```json
{ "types": ["sleep", "resting_heart_rate"], "days": 14 }
```

- `types`: one or more values from the closed allowlist. Unknown entries are
  dropped; an empty result returns an error string to the model, not data.
- `days`: clamped server-side to 1–30 (default 7). A larger request is clamped,
  not rejected.

The tool returns plain text aggregates — per type: number of days and samples
in the window, the daily average (a daily total for `steps`, `active_energy`,
`distance_walking_running`, `sleep`, `workout`; a daily mean for `heart_rate`,
`resting_heart_rate`, `body_mass`), the range across days, and the latest
reading with its timestamp.

This text is server-internal: it goes back to Claude as a `tool_result` inside
the turn and never appears in an iOS response field. Nothing in `/chat` or
`/healthkit/*` returns it. Shown here only so the behavior is reviewable:

```text
Health summary, last 14 day(s), aggregates only:
- resting_heart_rate: 14 day(s), 14 sample(s); avg 54 bpm; range 51 bpm–58 bpm; latest 52 bpm at 2026-08-15T06:40:00Z.
- sleep: 14 day(s), 61 sample(s); avg/day 7.1 h; range 5.4 h–8.3 h; latest 1.4 h at 2026-08-15T06:35:00Z.
Trends only — describe patterns, never diagnose a disease or condition; defer clinical questions to a clinician.
```

Guarantees:

- Raw samples are never serialized — no `sample_id`, no per-sample rows.
- The result is hard-capped at 2000 characters and truncated (keeping the
  closing rule) if it would exceed that.
- Reads are scoped to the calling user; an empty window says so instead of
  falling back to anything else.
- Failure returns an `Error [health_data_unavailable]` string so the turn
  continues without health data instead of erroring the chat.

### No medical diagnosis

Both the tool result and the Fitness and Diet mode instructions state the same
rule: describe trends only, never diagnose a disease or medical condition from
this data, and defer clinical questions to a clinician. This sits alongside the
existing epistemic and feasibility layers in `shared/epistemic.py`, which are
unchanged.

`get_recent_health_data` is a read. It does **not** create a `pending_action`
and is not a Confirm Gate action.
