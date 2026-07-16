# Lifesight Backend — Step 3: Conversation Memory + Confirm Gate Scaffolding

Builds on Step 2. `/chat` now takes a `conversation_id` and keeps multi-turn
history per conversation (in-memory, `CONVERSATIONS` in `main.py`) — the
model sees prior turns instead of treating every message as brand new. The
response shape also matches the frozen API contract shared with the iOS
app (see `.cursor/rules/10-api-contract.mdc` and `AGENTS.md`): the reply
field is now `reply` (was `response`), and every response includes
`conversation_id` and a `pending_action` slot.

A new `POST /confirm` endpoint resolves a pending action by id — this is
the Confirm Gate's second half. No mode populates `pending_action` yet
(no tool-calling is wired up), so it's always `null` today; the endpoint
and data shape exist and work, they just have nothing to confirm until a
mode actually proposes an irreversible action.

## Setup (run once)

1. Make sure you have Python 3.11+ installed.
2. In this folder, create a virtual environment and install dependencies:

   ```bash
   python3 -m venv venv
   source venv/bin/activate          # Mac/Linux
   pip install -r requirements.txt
   ```

   For contributing (secret-scan pre-commit hook, etc.), install dev deps instead:

   ```bash
   pip install -r requirements-dev.txt
   pre-commit install
   ```

3. Copy the example env file and add your real API key:

   ```bash
   cp .env.example .env
   ```

   Then open `.env` and replace the placeholder with your real Anthropic
   API key (starts with `sk-ant-...`). Get one from [console.anthropic.com](https://console.anthropic.com)
   if you don't have one yet.

4. Load the `.env` file before running (FastAPI doesn't do this
   automatically) — the easiest way is to export it in your shell:

   ```bash
   export $(cat .env | xargs)
   ```

   (On Windows/PowerShell this step is different — ask if you hit this.)

## Run it

```bash
uvicorn main:app --reload
```

You should see:

```
Uvicorn running on http://127.0.0.1:8000
```

## Test it

In a second terminal window, with the server still running:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"transcript": "Can you give me a quick recap of what you do?", "mode": "author"}'
```

You'll get back JSON like:

```json
{
  "reply": "...",
  "mode": "author",
  "conversation_id": "3f9c...",
  "pending_action": null
}
```

Copy the `conversation_id` from the response and send a follow-up in the
same conversation to confirm memory works — the reply should make sense as
a continuation, not a fresh answer:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"transcript": "Can you repeat the last thing you said, word for word?", "mode": "author", "conversation_id": "PASTE_THE_ID_HERE"}'
```

Leaving `conversation_id` out (or `null`) starts a fresh conversation with
a new id every time.

Try switching modes to confirm routing still works:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"transcript": "What do you help me with here?", "mode": "health"}'

curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"transcript": "What do you help me with here?", "mode": "jarvis"}'
```

Try an invalid mode to confirm error handling:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"transcript": "hi", "mode": "nonsense"}'
```

Should return a 400 error listing valid modes.

Try `/confirm` with a made-up action id to confirm 404 handling:

```bash
curl -X POST http://localhost:8000/confirm \
  -H "Content-Type: application/json" \
  -d '{"action_id": "does-not-exist", "approved": true}'
```

Should return a 404 — there's nothing to confirm yet since no mode creates
pending actions.

Also try:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/modes
```

Should return: `{"status":"ok"}` and `{"modes":["author","health","jarvis"]}`

## Project layout

```
main.py                  # /chat + /confirm, MODE_REGISTRY, in-memory state
requirements.txt
requirements-dev.txt     # + pre-commit, detect-secrets
shared/identity.py       # Olivia shared preamble
modes/
  author/prompt.py       # check / write / read-back
  health/prompt.py       # logs against plan
  jarvis/prompt.py       # Oliver's area, confirm-gate rules
docs/
  health-plan-reference.txt
AGENTS.md                 # cross-repo contract, shared with lifesight-ios
.cursor/rules/             # Cursor guardrails (architecture, contract, style)
.pre-commit-config.yaml    # detect-secrets hook
```

## What's NOT here yet (by design)

- No Google Docs reading/writing for Author Mode
- No tool-calling in any mode, so `pending_action` is always `null` — the
  Confirm Gate's shapes exist end-to-end, but nothing populates them yet
- No Postgres / durable storage — `CONVERSATIONS` and `PENDING_ACTIONS`
  are in-memory dicts that reset on every server restart
- No Calendar/Gmail for Jarvis Mode (Oliver's area)
- No auth (`Authorization: Bearer <token>`) — an open decision (Supabase
  vs in-house), not an oversight; see `.cursor/rules/10-api-contract.mdc`
- No auth/security on the endpoint itself (fine for local testing only —
  we'll need to lock this down before deploying anywhere public)

## If something goes wrong

- **"Could not resolve authentication method"** → your `.env` isn't loaded,
  or the API key is wrong/missing. Re-check step 3–4 above.
- **"unexpected keyword argument 'proxies'"** → a dependency version
  mismatch between `anthropic` and `httpx`. This `requirements.txt` already
  pins the working versions (`httpx==0.28.1`) — if you see this error,
  make sure you actually reinstalled after pulling the latest
  `requirements.txt` (`pip install -r requirements.txt` again).
- **Port 8000 already in use** → another process is using it; either stop
  it or run `uvicorn main:app --reload --port 8001` instead.
