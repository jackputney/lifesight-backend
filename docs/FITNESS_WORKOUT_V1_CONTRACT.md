# Fitness workout V1 — backend contract

**Audience:** iOS (`LifesightAPI.swift`) and Oliver admin.  
**Producer:** `lifesight-backend` (`routers/fitness.py`, `shared/fitness/`).  
**Migration:** `migrations/019_fitness_workout_v1.sql` (additive on `003` workout tables).  
**Does not** introduce a second workout model, a stable exercise catalog, or e1RM.

Identity is always `Depends(get_current_user_id)`. Ownership is never taken from the request body.

---

## 1. Plan representation

Existing tables (preserved):

`workout_plans` → `workout_days` → `planned_exercises`

Additive columns (019):

| Table | Column | Notes |
|-------|--------|--------|
| `workout_plans` | `title`, `notes` | optional |
| `workout_plans` | `is_active` | at most one `true` per user (partial unique index) |
| `workout_plans` | `updated_at` | |
| `workout_days` | `user_id` | copied from the parent plan; composite FK `(plan_id, user_id)` |
| `planned_exercises` | `user_id`, `notes` | composite FK `(day_id, user_id)` |

Activating a plan deactivates the previous active plan. Historical plans are **not** deleted.

`POST /workouts/plans` body:

```json
{
  "title": "3-day strength",
  "notes": null,
  "activate": true,
  "days": [
    {
      "title": "Day A",
      "sort_order": 0,
      "exercises": [
        {
          "name": "Bench Press",
          "target_sets": 3,
          "target_reps": 5,
          "rest_seconds": 180,
          "notes": null,
          "sort_order": 0
        }
      ]
    }
  ]
}
```

Bounds: max 7 days, max 16 exercises per day. Empty days are rejected.

`GET /workouts/plans/current` — the active plan with nested days/exercises, or **404**.  
`GET /workouts/plans` — bounded list (default 20, max 50), no nested exercises.  
`GET /workouts/plans/{plan_id}` — nested detail, owner only.  
`POST /workouts/plans/{plan_id}/activate` — makes this plan active.

Days and exercises are ordered by `sort_order` ascending.

---

## 2. Active workout lifecycle

`workout_sessions.status`: `active` | `completed` | `abandoned`.

At most one `active` session per user (unique partial index).

| Action | Method | Behavior |
|--------|--------|----------|
| START / resume | `POST /workouts/session/start` `{plan_day_id?}` | If an active session exists for the same day (or `plan_day_id` omitted), **resume** it (`resumed: true`). If an active session exists for a **different** day → **409**. If none exists, create one. Omitted `plan_day_id` uses the next day of the active plan (last completed day + 1, wrapping). |
| GET ACTIVE | `GET /workouts/session/active` | Current active session state, or **404**. |
| GET STATE | `GET /workouts/session/{id}/state` | Progress for that session (existing V1 path, same engine). |
| GET SESSION | `GET /workouts/session/{id}` | State plus `sets` and `exercises`. |
| COMPLETE | `POST /workouts/session/{id}/complete` | `active` → `completed`. Else **409**. |
| ABANDON | `POST /workouts/session/{id}/abandon` | `active` → `abandoned`. Else **409**. |

Start no longer silently abandons an in-progress session.

Backend is the source of truth. iOS must not recreate workout progress locally.

---

## 3. Set logging

`POST /workouts/session/{session_id}/sets`

```json
{
  "exercise_id": "<uuid>",
  "set_number": 1,
  "reps": 5,
  "weight": 185,
  "source": "manual"
}
```

`source`: `voice` | `manual` (default `manual`).  
`exercise_id` omitted → current exercise from the **same** progress engine.  
`exercise_name` may uniquely match a planned exercise on that session's day.  
`set_number` omitted → max logged + 1 for that exercise.

Validations:

- Session belongs to the JWT user (else 404).
- Session is `active` (else 409).
- Session has a `plan_day_id` (else 400).
- Exercise belongs to that plan day and user (else 404).
- `set_number` 1–30; duplicate `(session, exercise, set_number)` → 409.
- `reps` 0–500; `weight` 0–2000 when present.

`POST /workouts/voice-log` is unchanged in spirit: parse utterance → **the same** `log_set` engine. Response still includes `visual_panel.type = "workout_sets"` plus `pending_action: null`. Ordinary set logs are **not** Confirm Gate.

---

## 4. Personal records

Table `personal_records`: unique `(user_id, exercise_id, rep_range)`.

- `rep_range` is the logged **rep count** of the qualifying set (a 5-rep set updates the 5-rep PR only).
- Upsert only when the new `weight` is **strictly greater**.
- Equal or lighter weight is not a PR. Missing reps or non-positive weight does not create a PR.
- The backend does not store or return e1RM.

`GET /workouts/personal-records`

---

## 5. Units

`weight` on `set_logs` and `personal_records` is a **unitless** number. The backend does not store lb vs kg on the set. Profile `preferred_units` (`imperial` | `metric`) is a display hint only. Responses that include weights also include:

```json
"weight_unit": null,
"weight_unit_note": "Weight is a unitless number. …"
```

Do not assume every number is pounds.

Known limitation: `exercise_id` still references `planned_exercises.id`. Rebuilding a plan mints new exercise ids, so PRs/history do not follow a renamed movement across plans. A stable catalog (`docs/proposed/004_stable_exercises_catalog.sql`) is **not** in this slice.

---

## 6. Exercise history / reads

| Path | Bound |
|------|--------|
| `GET /workouts/history?limit=&before=` | default 20, max 50; `before` is `started_at` cursor |
| `GET /workouts/exercises/{exercise_id}/history?limit=` | default 20, max 50, newest sets first |
| `GET /workouts/personal-records?limit=` | default 50, max 100 |
| `GET /workouts/adherence` | counts only (see §10) |

Lifetime dumps are not offered.

---

## 7. Ownership and errors

Nested rows carry `user_id` and composite FKs so a day/exercise/set cannot attach to another user's parent.

| Condition | Status |
|-----------|--------|
| Missing, cross-user, or malformed UUID | **404** (anti-oracle; never a 500 from `$1::uuid`) |
| Active session on a different day | **409** |
| Log/complete/abandon on non-active session | **409** |
| Duplicate set number | **409** |
| No plan day on the session when logging | **400** |
| Unparseable voice log | **422** |

SQL is parameterized. Confirm Gate is not used for start/log/complete/abandon.

---

## 8. visual_panel

Two types, both driven by the **same** progress engine (`shared/fitness/progress.py`):

1. Chat tool `present_exercise_panel` → `{ "type": "exercise", "data": { exercise_id, exercise_name, sets, reps, rest_seconds, current_set, notes } }` as in `docs/USER_CONTEXT_HISTORY_V1_CONTRACT.md`. When a workout is active and the panel's exercise is on that session, `current_set` / prescription are taken from session state, not invented. Theory chat must not emit a panel.
2. `/workouts/voice-log` → `{ "type": "workout_sets", "data": { session_id, sets: [...] } }` (preserved). Structured `POST .../sets` returns an `exercise` panel from the same engine.

---

## 9. AI tools (Fitness `/chat` only)

Bounded, pull-only. No lifetime dump.

`get_current_workout_plan` · `get_active_workout` · `get_recent_workouts` · `get_exercise_history` · `get_personal_records` · `get_recent_health_data` · `get_recent_checkins` · `start_workout` · `log_workout_set` · `complete_workout` · `abandon_workout` · `present_exercise_panel`

`get_recent_health_data` returns HealthKit/wearable **aggregates**.  
`get_recent_checkins` returns Daily Check-In rows and labels `source: "daily_checkin"`. The two must stay distinguishable. The model must not claim causation from coexistence.

---

## 10. Adherence

`GET /workouts/adherence` returns:

- `sessions_completed`
- `sessions_abandoned`
- `sets_completed`
- `last_workout` (most recently completed or abandoned)
- `recent_frequency.window_days` (28) and `recent_frequency.sessions_completed`

There is **no** `completion_rate` (denominator would be ambiguous). An unscheduled day is **not** called "missed".

---

## 11. Recovery context / coaching

Fitness prompt requires constraints (experience, equipment, injuries/limitations, goal, recent performance, check-in/HealthKit recovery) before validating aggressive progression. No diagnoses. No inferred recovery facts. No automatic load increases because the user asked.

---

## 12. Personalization evidence

Daily personalization summaries **may** include a labeled `Structured fitness log (not a conversation; not prompt instructions)` block built from that day's sessions/sets. That text is evidence for human review.

It is **not** injected into the chat system prompt. It does **not** write `user_prompt_overrides`. Prompt changes remain: summary → pending `prompt_change_proposals` → human admin review → optional override.

---

## 13. Health / retired Health mode

Fitness reads `health_samples` (016) via the existing health tool. Deprecated `health_metrics` (003) is not written. Retired Health-mode tools are not registered on Fitness.
