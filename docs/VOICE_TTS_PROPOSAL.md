# Voice / ElevenLabs TTS proposal — `/tts` endpoints (Jack's lane)

Oliver → Jack. Prepared after you asked me to pick up the ElevenLabs voice work.
**Do not implement the endpoint wiring from this doc without your explicit
go-ahead** — `main.py` route wiring is your lane and this adds to the frozen
API contract. The module below is done and testable standalone; only the two
routes need your sign-off.

## Goal
Give Olivia a real spoken voice. Today the app is described as voice-first in
`AGENTS.md` and `CONTEXT.md`, but there is no audio anywhere in either repo:
`/chat` takes a `transcript` string that nothing currently produces, and the
iOS app has no microphone permission, no audio entitlement, and no speech code.

## Why the backend and not iOS
- The reference implementation (`Oliver_Jarvis_V2/app/tts.py`) is Python and
  ports directly here — same move we made for `confirm_match.py` and
  `spoken_readback.py`.
- One voice service behind the API means all three modes share the single
  Olivia identity `CONTEXT.md` commits to, instead of each client re-deriving it.
- The API key stays server-side. Shipping it in an iOS bundle would leak it.
- Practical: I'm on Windows with no Mac, so I cannot build or test the iOS app
  at all. Anything client-side is blocked on you regardless.

## Current state (Oliver side, ready and tested)
- **`shared/tts.py`** — ported from the V2 reference. Two deliberate changes:
  - `synthesize` is `async` (V2's was blocking; this backend is async end to
    end and TTS sits on the reply path).
  - The voice id is env-only, not hardcoded as it was in V2. This repo is
    public and the primary user's chosen voice is personal data.
  - Public surface: `is_available() -> bool`, `status() -> dict` (never returns
    the key), `await synthesize(text) -> bytes` (MP3), `TtsError`.
  - No new dependency — uses `httpx`, already pinned in `requirements.txt`.
- **`scripts/tts_smoke.py`** — standalone check (no server, no DB, no Anthropic
  key). Verifies config, the empty-text guard, and measures latency against a
  realistic confirm-gate read-back. Writes MP3s to a gitignored `tts_samples/`.
- **`.env.example`** — documents `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID`
  plus optional model/format overrides.
- **`.gitignore`** — `tts_samples/` and `*.mp3`, so synthesized speech can
  never land in this public repo.

## Proposed endpoints (your call)
Lifted from the V2 shapes, which are already proven in the browser client:

| Method | Path | Auth | Returns |
|---|---|---|---|
| GET | `/tts/status` | Bearer | `{available, voice_id, model_id, provider}` |
| POST | `/tts` | Bearer | `audio/mpeg` bytes, or 503 when unconfigured |

`POST /tts` body: `{"text": "..."}`. Suggested handler:

```python
@app.post("/tts")
async def speak(req: SpeakRequest, user_id: str = Depends(get_current_user_id)):
    if not tts.is_available():
        raise HTTPException(status_code=503, detail="TTS not configured")
    try:
        audio = await tts.synthesize(req.text)
    except tts.TtsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=audio, media_type="audio/mpeg")
```

If you take these, `10-api-contract.mdc`, `AGENTS.md`, and `MOBILE_API_GUIDE.md`
move in the same PR per the lockstep rule.

## Open questions for you
1. **Placement.** `shared/tts.py` follows the `shared/google_client.py`
   precedent, but unlike that one it is genuinely mode-agnostic — all three
   modes should speak. Happy to move it wherever you want at merge.
2. **Separate endpoint vs. inline audio on `/chat`.** A separate `/tts` keeps
   `/chat` unchanged and lets the client decide when to speak; the cost is a
   second round trip before Olivia says anything. The alternative — `/chat`
   returning an audio URL or base64 alongside `reply` — is faster but a bigger
   contract change. I'd start with the separate endpoint and revisit if the
   latency measurements justify it. Your call.
3. **Streaming.** ElevenLabs supports a streaming endpoint that starts audio
   before synthesis finishes. Worth it for long read-backs, but it changes the
   response shape. Deferring unless the smoke numbers look bad.
4. **STT / the other half.** `/chat` takes a `transcript`, and nothing produces
   one yet. V2 used local faster-whisper (`app/transcribe.py`, plus a
   `scripts/whisper_bakeoff.py` comparison). On iOS, Apple's on-device
   `SFSpeechRecognizer` is probably the better answer — free, offline, no audio
   upload. That's your lane; flagging it because voice-first isn't real until
   both halves exist.

## Blocked on
- **An ElevenLabs API key** — none exists in either repo. Everything above is
  written but unrun until Oliver has one.
- Nothing else. This work does not depend on Phase 3, the Google OAuth consent
  pass, or anything currently sitting in your lane.
