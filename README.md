# Lifesight Backend

Voice-first backend for the Lifesight iOS app. Handles `/chat` requests from the app, routes by mode, and calls Claude with mode-specific system prompts and tools.

## Modes

| Mode | Purpose |
|------|---------|
| **author** | Manuscript check, compose, read-back (Google Docs) |
| **health** | Diet, workouts, weigh-ins — logged against uploaded plan |
| **jarvis** | Calendar + email — confirm-gated on all writes |

Step 1 skeleton: only **author** is wired in `main.py`. The `modes/` directory has prompt scaffolding ready for the next step.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # add your ANTHROPIC_API_KEY
```

## Run

```bash
uvicorn main:app --reload --port 8000
```

## Test

```bash
curl -s http://localhost:8000/health

curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"transcript": "What can you help me with in author mode?", "mode": "author"}'
```

## Project layout

```
main.py                  # /chat endpoint (Step 1 — hardcoded author prompt)
requirements.txt
shared/identity.py       # Olivia shared preamble
modes/
  author/prompt.py       # check / write / read-back
  health/prompt.py       # logs against plan
  jarvis/prompt.py       # Oliver's area, confirm-gate rules
docs/
  health-plan-reference.txt
```

## Next step

Wire `main.py` to import system prompts from `modes/` instead of the hardcoded `AUTHOR_SYSTEM_PROMPT`.
