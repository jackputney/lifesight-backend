# LifeSight — Architecture & Decisions (v2)

**Naming (settled):** the product is **LifeSight**; the AI agent/voice persona is
**Olivia**. Chat mode keys (v2): `fitness`, `diet`, `author`. `jarvis` remains
in the repo as an inert registry entry (not actively developed this pass).
`settings` is an iOS screen, not a chat mode.

Voice-first assistant for a near-blind primary user. Accessibility (VoiceOver,
spoken confirmation) remains a dominating constraint — Confirm Gate still
exists, but its scope is narrowed (see below).

## Confirmed decisions (v2 — authoritative)

**Auth — Supabase, multiple login options.** Supabase Auth owns identity;
`auth.users(id)` (UUID) is the FK for every table. Login supports
**email/password (or magic link) and Sign in with Apple**, both presented at
the login screen. This knowingly reverses the earlier Apple-only / no-password
decision. Endpoints get identity **only** via `Depends(get_current_user_id)`.
Auth HTTP is proxied through `/auth/*` so iOS talks only to this backend.
`AUTH_MODE=dev` (default) resolves to a fixed dev UUID; `AUTH_MODE=real`
verifies the Supabase JWT.

**Modes — fitness / diet / author.** The old `author` / `health` / `jarvis`
trio as the active product set is retired. `health` is superseded by
`fitness` + `diet`. `jarvis` code stays untouched and registered but is not
actively developed this pass.

**Author — Postgres-native.** Manuscripts → chapters → scenes live in
Postgres. Google Docs is **not** the source of truth on `v2-rebuild`. The
Docs/OAuth implementation remains recoverable on `main` @ `f3d97158`.

**Confirm Gate — irreversible/destructive only.** Shared `pending_actions`
table remains. It guards food entry saves, destructive manuscript actions
(e.g. delete scene), and similar. Ordinary workout set logs and reversible
scene edits do **not** create pending actions.

**Wearables — Terra API (default).** Aggregator for Apple Watch, Oura, etc.
via one integration. Spike may be reconsidered before building alternatives;
flag before switching.

**API contract — additive `visual_panel`.** `/chat` (and some domain
endpoints) may return `visual_panel: {type, data} | null` for inline
quarter-screen UI. Existing `{reply, mode, conversation_id, pending_action}`
consumers remain valid when the field is absent/null.

**Sync — LWW by default for log-style rows.** `food_entries`, `set_logs`,
`health_metrics` are independent rows. Author content is structured Postgres
CRUD (not Docs merge). Legacy `writing_*` tables from the Docs era remain in
schema history; new code must not write to them.

## Build status (v2-rebuild branch)
- [x] Migration `003_v2_rebuild.sql` (fitness, diet, author, wearables).
- [x] Auth proxy routes `/auth/signup|login|magic-link|apple`.
- [x] Fitness `/workouts/*`, Diet `/food/*`, Author manuscripts + brainstorm,
      Wearables Terra connect + webhook.
- [x] `MODE_REGISTRY` → fitness / diet / author (+ inert jarvis).
- [x] `visual_panel` on `ChatResponse`.
- [ ] Live `AUTH_MODE=real` verification against a production Supabase project.
- [ ] End-to-end Terra + real wearable device supervised test.

## Branching
All v2 work lives on `v2-rebuild`. Do not force-push or rewrite `main`.
`main` @ `f3d97158` preserves the Author Google Docs WIP.
