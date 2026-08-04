"""Standalone ElevenLabs check — no server, no DB, no Anthropic key needed.

Verifies shared/tts.py against the real API: config present, synthesis works,
and latency is acceptable for a voice-first reply. Writes the MP3s so you can
listen and judge whether the voice is right for the primary user.

    python scripts/tts_smoke.py
    python scripts/tts_smoke.py --text "Custom phrase to speak."

Exits non-zero if anything fails, so it can gate a later CI step.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from shared import tts  # noqa: E402  (import after sys.path/env setup)

OUT_DIR = Path(__file__).resolve().parent.parent / "tts_samples"

# Short line first (latency floor), then a realistic confirm-gate read-back —
# the longest thing Olivia routinely says, so it sets the worst-case wait.
PHRASES = [
    ("short", "Ready."),
    (
        "readback",
        "Just to confirm before I send it: an email to your editor, subject "
        "'Chapter four draft', saying the revised chapter is attached and "
        "you'd like notes by Friday. Say 'confirm' to send.",
    ),
]


async def run(custom_text: str | None) -> int:
    print("=== config ===")
    st = tts.status()
    for k, v in st.items():
        print(f"  {k}: {v}")
    if not st["available"]:
        print(
            "\nFAIL: not configured. Set ELEVENLABS_API_KEY and "
            "ELEVENLABS_VOICE_ID in .env, then re-run.",
            file=sys.stderr,
        )
        return 1

    print("\n=== error handling ===")
    try:
        await tts.synthesize("   ")
        print("  empty-text guard: NOT RAISED  <-- BAD")
        return 1
    except tts.TtsError as exc:
        print(f"  empty-text guard: raised TtsError ({exc})")

    phrases = [("custom", custom_text)] if custom_text else PHRASES

    OUT_DIR.mkdir(exist_ok=True)
    print(f"\n=== synthesis (writing to {OUT_DIR}) ===")
    failed = False
    for name, text in phrases:
        started = time.perf_counter()
        try:
            audio = await tts.synthesize(text)
        except tts.TtsError as exc:
            print(f"  {name:<9} FAILED: {exc}")
            failed = True
            continue
        elapsed = time.perf_counter() - started
        path = OUT_DIR / f"{name}.mp3"
        path.write_bytes(audio)
        verdict = "ok" if elapsed < 2.0 else "SLOW for a voice reply"
        print(
            f"  {name:<9} {len(audio):>7,} bytes  {elapsed:5.2f}s  {verdict}"
            f"  -> {path.name}"
        )

    if failed:
        return 1
    print("\nPASS — listen to the files above and confirm the voice is right.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="speak this instead of the built-in phrases")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args.text)))
