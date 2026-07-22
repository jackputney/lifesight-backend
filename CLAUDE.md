# CLAUDE.md — lifesight-backend

Voice-first assistant (product: LifeSight, persona: Olivia) for a near-blind
primary user. FastAPI + Supabase Postgres. Three modes: author / health /
jarvis. Accessibility is the dominating constraint.

Read in this order before non-trivial work:
1. `AGENTS.md` — cross-repo contract with `lifesight-ios` (frozen API shapes)
2. `docs/OWNERSHIP.md` — who owns which files; Oliver = Jarvis lane on the
   `jarvis-oauth` branch, Jack = everything else on `main`
3. `docs/JARVIS_PLAN.md` — Jarvis status + next tasks
4. `CONTEXT.md` — frozen architecture decisions (auth, Confirm Gate, sync)
5. `.cursor/rules/` — 00-core (escalation protocol), 10-api-contract,
   30-jarvis-lane — these bind Claude Code sessions the same as Cursor

Hard rules that override convenience:
- This repo is PUBLIC. No secrets or real personal data in code/commits/docs.
- Jarvis-lane work: `jarvis-oauth` branch only, never `main`.
- Confirm Gate fronts every irreversible action — no exceptions.
- `shared/confirm_match.py` / `shared/spoken_readback.py` are verbatim ports;
  do not modify.
- Cross-lane changes are written proposals to the lane owner, not edits.

Run: venv at `./venv`, `uvicorn main:app --reload`, needs `.env` (see
`.env.example`; `DATABASE_URL` required at startup). Migrations:
`python scripts/run_migrations.py --seed-dev-user`.
