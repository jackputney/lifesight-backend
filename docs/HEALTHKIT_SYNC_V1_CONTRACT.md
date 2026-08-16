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
| workout | canonical `min`; same accepted units as `sleep`. Categorical: may instead send `value_text` (e.g. the activity name) with `value` and `unit` omitted |

Only `sleep` and `workout` may be stored without a unit, and only when they
carry `value_text`. Every sample must have at least one of `value` or
`value_text`.

## 2. `POST /healthkit/sync`

Requires `Authorization: Bearer <access JWT>`. The body never carries a
`user_id` — ownership is the token's user.

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
| sample_id | required, 1–200 chars. The HealthKit sample UUID. This is the dedupe key |
| type | required, one of the closed allowlist above |
| start_at | required ISO 8601; `Z` accepted. Naive timestamps are read as UTC |
| end_at | required ISO 8601, must be `>= start_at` |
| value | optional number. Required unless the type is categorical and `value_text` is set |
| unit | optional string, `<=32`. Required whenever `value` is set |
| value_text | optional string, `<=120` — categorical payload for `sleep` / `workout` |
| source_bundle | optional string, `<=200` |
| source_name | optional string, `<=200` |

Response:

```json
{
  "accepted": 2,
  "updated": 0,
  "ignored": 0,
  "server_time": "2026-08-15T14:05:12.481Z"
}
```

| Field | Notes |
|-------|--------|
| accepted | rows newly inserted this request |
| updated | rows that already existed under this `sample_id` and whose stored content changed |
| ignored | everything else: samples rejected by validation, duplicate `sample_id`s inside the same batch, and re-sent samples identical to what is already stored |
| server_time | when this sync completed, server clock. Same value written to `last_synced_at` |

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

One malformed sample never fails the batch. Each of these is counted in
`ignored` and the rest of the batch still lands:

- `type` not in the allowlist
- `unit` missing on a numeric sample, or not accepted for that `type`
- `end_at` earlier than `start_at`
- `start_at` or `end_at` not parseable as ISO 8601
- neither `value` nor `value_text` present, or `value` not a finite number
- blank `sample_id`

### Errors

| Status | When |
|--------|--------|
| 400 | more than 1000 samples in one request. Nothing is written; split the batch |
| 401 | missing, malformed, or expired bearer token |
| 422 | body is not shaped like the request above (e.g. `samples` missing, a field over its length limit) |
| 503 | database temporarily unavailable (standard backend-wide shape) |

## 3. `GET /healthkit/status`

Requires the same bearer token. Returns counts and timestamps only — it never
returns a sample, a `sample_id`, or any other user's data.

```json
{
  "last_synced_at": "2026-08-15T14:05:12.481Z",
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
| last_synced_at | ISO 8601 or `null` — when `POST /healthkit/sync` last completed for this user. Read from `health_sync_state`, not derived from samples, so a sync that uploaded only known samples still counts |
| categories | always contains **all eight** allowlisted types, in the allowlist order, so the client decodes a fixed shape |
| categories[type].latest_sample_at | ISO 8601 or `null` — `MAX(start_at)` over all of this user's samples of that type, any provider |
| categories[type].count_last_30d | integer, `0` when there is nothing. Counts samples with `start_at` within the last 30 days |

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
`start_at == end_at == recorded_at`. Terra metric keys outside the mapping are
dropped and counted rather than inventing a `sample_type`.

The webhook response keeps its shape and adds one counter:

```json
{ "ok": true, "written": 6, "ignored": 3 }
```

`written` is inserted + updated rows; `ignored` is unmapped Terra metrics plus
replayed rows that changed nothing (so a replay returns `written: 0`).
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
reading with its timestamp:

```json
{
  "tool_result": "Health summary, last 14 day(s), aggregates only:\n- resting_heart_rate: 14 day(s), 14 sample(s); avg 54 bpm; range 51 bpm–58 bpm; latest 52 bpm at 2026-08-15T06:40:00Z.\n- sleep: 14 day(s), 61 sample(s); avg/day 7.1 h; range 5.4 h–8.3 h; latest 1.4 h at 2026-08-15T06:35:00Z.\nTrends only — describe patterns, never diagnose a disease or condition; defer clinical questions to a clinician."
}
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
