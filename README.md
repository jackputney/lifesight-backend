# Lifesight Backend — Step 2: Mode Router

Builds on Step 1. The `/chat` endpoint now takes a `mode` field
("author" / "health" / "jarvis") and routes to the right system prompt
via `MODE_REGISTRY` in `main.py`. Every mode shares Olivia's identity
(`shared/identity.py`) with mode-specific instructions layered on top
(`modes/<mode>/prompt.py`).

Still no tools wired up to actually execute anything yet (no Google Docs,
no Postgres, no Calendar/Gmail) — that's the next step, one mode at a
time. This step only proves mode routing itself works correctly.

## Setup (run once)

1. Make sure you have Python 3.11+ installed.
2. In this folder, create a virtual environment and install dependencies:

   ```bash
   python3 -m venv venv
   source venv/bin/activate          # Mac/Linux
   pip install -r requirements.txt
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

You should get back a JSON response with Claude's reply, speaking as
Olivia, in Author Mode (mentioning check/write/read-back).

Try switching modes to confirm routing works:

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

Also try:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/modes
```

Should return: `{"status":"ok"}` and `{"modes":["author","health","jarvis"]}`

## Project layout

```
main.py                  # /chat endpoint + MODE_REGISTRY
requirements.txt
shared/identity.py       # Olivia shared preamble
modes/
  author/prompt.py       # check / write / read-back
  health/prompt.py       # logs against plan
  jarvis/prompt.py       # Oliver's area, confirm-gate rules
docs/
  health-plan-reference.txt
```

## What's NOT here yet (by design — this is step 2 of the build order)

- No Google Docs reading/writing for Author Mode (step 3)
- No Confirm Gate (step 4)
- No Postgres / health logging for Health Mode
- No Calendar/Gmail for Jarvis Mode (Oliver's area)
- No memory between requests — every `/chat` call is a fresh conversation
  with no knowledge of previous messages
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
