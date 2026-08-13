# User context + history V1 — iOS contract handoff

Additive backend contracts on branch `feature/user-context-history-v1`.
Existing `/chat` fields remain: `reply`, `mode`, `conversation_id`,
`pending_action`, `visual_panel`, `research`, `client_actions`.

Migrations: `010_user_profiles.sql`, `011_conversation_context.sql`
(next free number after main's `009`).

## 1. `visual_panel` — Fitness `exercise`

```json
{
  "type": "exercise",
  "data": {
    "exercise_id": null,
    "exercise_name": "Bench Press",
    "sets": 4,
    "reps": 8,
    "rest_seconds": 120,
    "current_set": 1,
    "notes": null
  }
}
```

- Emitted from Fitness `/chat` only when Claude calls tool `present_exercise_panel`
  after a concrete exercise prescription is established.
- `exercise_id` is a UUID or `null` — never invented.
- Ordinary fitness chat → `visual_panel: null`.
- `/workouts/voice-log` continues to use `workout_sets` (unchanged this sprint).

## 2. Profile — `GET/PATCH /profile`

Identity remains `/auth/me` (`display_name`, `email`, …).

`GET /profile` / `PATCH /profile` (JWT owner only; no body `user_id`):

| Field | Notes |
|-------|--------|
| timezone | IANA string |
| date_of_birth | date |
| height_cm / weight_kg | numbers |
| interaction_style | `standard` \| `voice_first` \| `high_accessibility` |
| vision_preference | nullable string |
| spoken_response_preference | nullable string |
| experience_level | nullable string |
| primary_goals | string[] (max 20) |
| training_frequency | nullable string |
| available_equipment | string[] |
| injuries_limitations | nullable text |
| nutrition_goal | nullable string |
| dietary_preferences | string[] |
| allergies_restrictions | string[] |

Missing `user_profiles` row → safe nulls / empty arrays. `display_name` may be
echoed for convenience but is stored only on `users`.

## 3. Chat history

- `GET /conversations?limit=&cursor=`
- `GET /conversations/{id}` → includes **authoritative `mode`**
- `GET /conversations/{id}/messages?limit=&before_seq=`
- Resume: existing `POST /chat` with `conversation_id` (stored mode wins)

Title V1: first substantive user message (~60 chars) else `"{Mode} chat"`.

## 4. `client_actions` — `open_conversation`

```json
{ "type": "open_conversation", "conversation_id": "<uuid>" }
```

Not Confirm Gate. Unique match only; ambiguous → clarify + `[]`.
Navigate V1 unchanged: `{ "type": "navigate", "target": "..." }`.

## 5. Context budget + summaries (server-only)

Env (defaults):

- `CONTEXT_INPUT_TOKEN_BUDGET=24000`
- `CONTEXT_RECENT_MESSAGE_CAP=20`
- `CONTEXT_SUMMARY_THRESHOLD=30`
- `CONTEXT_SUMMARY_TOKEN_BUDGET=1500` (hard cap on stored `summary_text`)
- `CONTEXT_CHARS_PER_TOKEN=4`

Model call = system + compact profile + rolling summary + recent raw window
(capped and trimmed to budget) + current turn. Full raw history stays in Postgres.
Summary failure never fails `/chat`.

## 6. Admin / seed

No public `/admin/*`. Local/staging: `scripts/seed_user_profile.py` with
`ADMIN_SEED_TOKEN`, refuses production unless
`ALLOW_PROFILE_SEED_IN_PRODUCTION=1`. Writes `admin_audit_log`.
