"""Cached stalling audio — closed allowlist, truthful mapping, cache reuse.

Run:  python -m unittest tests.test_stall_audio_v1 -v
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared import stall_audio
from shared.stall_audio import (
    STALL_AUDIO_CACHE_VERSION,
    STALL_KEY_CALENDAR_CHECK,
    STALL_KEY_GENERAL_TOOL_CHECK,
    STALL_KEY_HEALTH_CHECK,
    STALL_KEY_MAIL_CHECK,
    STALL_PHRASES,
    StallAudioError,
    UnknownStallKeyError,
    cache_path,
    get_stall_audio,
    stall_key_for_tool,
    stall_keys,
    stall_phrase,
)

VOICE_ID = "voice-test-id"


def _env(cache_dir: str, **overrides: str):
    base = {
        "AUTH_MODE": "dev",
        "APP_ENV": "test",
        "STALL_AUDIO_CACHE_DIR": cache_dir,
        "ELEVENLABS_API_KEY": "el-test-key",  # pragma: allowlist secret
        "ELEVENLABS_VOICE_ID": VOICE_ID,
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class _FakeTTS:
    """Stands in for shared.elevenlabs_tts.stream_speech_mp3 (no network)."""

    def __init__(self, audio: bytes = b"ID3-fake-mp3"):
        self.audio = audio
        self.calls: list[str] = []

    async def __call__(self, text: str):
        self.calls.append(text)
        audio = self.audio

        async def _gen():
            yield audio

        return _gen()


class StallAllowlistTests(unittest.TestCase):
    def test_allowlist_is_exactly_four_keys(self):
        self.assertEqual(
            stall_keys(),
            frozenset(
                {
                    STALL_KEY_CALENDAR_CHECK,
                    STALL_KEY_HEALTH_CHECK,
                    STALL_KEY_MAIL_CHECK,
                    STALL_KEY_GENERAL_TOOL_CHECK,
                }
            ),
        )

    def test_exact_phrase_text(self):
        self.assertEqual(stall_phrase(STALL_KEY_CALENDAR_CHECK), "Let me check your calendar.")
        self.assertEqual(
            stall_phrase(STALL_KEY_HEALTH_CHECK), "I'm checking your latest health data."
        )
        self.assertEqual(stall_phrase(STALL_KEY_MAIL_CHECK), "Let me look at your mail.")
        self.assertEqual(
            stall_phrase(STALL_KEY_GENERAL_TOOL_CHECK), "One moment while I pull that up."
        )

    def test_no_fake_progress_phrase(self):
        for phrase in STALL_PHRASES.values():
            lowered = phrase.lower()
            self.assertNotIn("almost done", lowered)
            self.assertNotIn("nearly", lowered)
            self.assertNotIn("finishing", lowered)

    def test_arbitrary_text_is_rejected(self):
        for candidate in (
            "Almost done!",
            "Reading your email about the biopsy results",
            "",
            "calendar_check ",
            "../../etc/passwd",
        ):
            with self.assertRaises(UnknownStallKeyError):
                stall_phrase(candidate)

    def test_non_string_key_rejected(self):
        with self.assertRaises(UnknownStallKeyError):
            stall_phrase(None)  # type: ignore[arg-type]


class StallTruthfulnessTests(unittest.TestCase):
    def test_tool_mapping_matches_work_being_started(self):
        self.assertEqual(stall_key_for_tool("list_calendar_events"), STALL_KEY_CALENDAR_CHECK)
        self.assertEqual(stall_key_for_tool("get_recent_health_data"), STALL_KEY_HEALTH_CHECK)
        self.assertEqual(stall_key_for_tool("send_email"), STALL_KEY_MAIL_CHECK)
        self.assertEqual(stall_key_for_tool("gmail_list_threads"), STALL_KEY_MAIL_CHECK)
        self.assertEqual(stall_key_for_tool("mail_search"), STALL_KEY_MAIL_CHECK)

    def test_unknown_tool_falls_back_to_general(self):
        for name in ("create_pending_action", "present_exercise_panel", "", "weird_tool"):
            self.assertEqual(stall_key_for_tool(name), STALL_KEY_GENERAL_TOOL_CHECK)

    def test_calendar_tool_never_claims_mail_or_health(self):
        key = stall_key_for_tool("list_calendar_events")
        self.assertNotIn("mail", stall_phrase(key).lower())
        self.assertNotIn("health", stall_phrase(key).lower())


class StallCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = self._tmp.name
        stall_audio._locks.clear()
        self.addCleanup(stall_audio._locks.clear)

    def _files(self) -> list[Path]:
        return sorted(p for p in Path(self.cache_dir).rglob("*") if p.is_file())

    def test_cache_path_is_version_and_voice_keyed(self):
        with _env(self.cache_dir):
            path = cache_path(STALL_KEY_MAIL_CHECK, voice_id=VOICE_ID)
        self.assertEqual(path.name, "mail_check.mp3")
        self.assertEqual(path.parent.name, VOICE_ID)
        self.assertEqual(path.parent.parent.name, STALL_AUDIO_CACHE_VERSION)

    def test_cache_path_rejects_unknown_key(self):
        with _env(self.cache_dir):
            with self.assertRaises(UnknownStallKeyError):
                cache_path("please synthesize this", voice_id=VOICE_ID)

    def test_second_call_reuses_cache_and_does_not_resynthesize(self):
        fake = _FakeTTS()
        with _env(self.cache_dir):
            with patch("shared.stall_audio.stream_speech_mp3", new=fake):
                first = asyncio.run(get_stall_audio(STALL_KEY_CALENDAR_CHECK))
                second = asyncio.run(get_stall_audio(STALL_KEY_CALENDAR_CHECK))
        self.assertEqual(first, b"ID3-fake-mp3")
        self.assertEqual(second, first)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0], "Let me check your calendar.")

    def test_concurrent_first_calls_synthesize_once(self):
        fake = _FakeTTS()

        async def _race():
            stall_audio._locks.clear()
            return await asyncio.gather(
                get_stall_audio(STALL_KEY_MAIL_CHECK),
                get_stall_audio(STALL_KEY_MAIL_CHECK),
                get_stall_audio(STALL_KEY_MAIL_CHECK),
            )

        with _env(self.cache_dir):
            with patch("shared.stall_audio.stream_speech_mp3", new=fake):
                results = asyncio.run(_race())
        self.assertEqual(results, [b"ID3-fake-mp3"] * 3)
        self.assertEqual(len(fake.calls), 1)

    def test_only_allowlisted_phrases_reach_the_provider(self):
        fake = _FakeTTS()
        with _env(self.cache_dir):
            with patch("shared.stall_audio.stream_speech_mp3", new=fake):
                for key in sorted(stall_keys()):
                    asyncio.run(get_stall_audio(key))
        self.assertEqual(sorted(fake.calls), sorted(STALL_PHRASES.values()))

    def test_private_text_is_never_written_to_the_cache(self):
        fake = _FakeTTS()
        private_text = "Your biopsy results came back from Dr. Rivera"
        with _env(self.cache_dir):
            with patch("shared.stall_audio.stream_speech_mp3", new=fake):
                with self.assertRaises(UnknownStallKeyError):
                    asyncio.run(get_stall_audio(private_text))
                asyncio.run(get_stall_audio(STALL_KEY_HEALTH_CHECK))

        self.assertEqual(fake.calls, ["I'm checking your latest health data."])
        files = self._files()
        self.assertEqual([p.name for p in files], ["health_check.mp3"])
        for path in files:
            self.assertNotIn("biopsy", path.read_bytes().decode("utf-8", "ignore"))

    def test_no_partial_files_remain_after_write(self):
        fake = _FakeTTS()
        with _env(self.cache_dir):
            with patch("shared.stall_audio.stream_speech_mp3", new=fake):
                asyncio.run(get_stall_audio(STALL_KEY_GENERAL_TOOL_CHECK))
        self.assertEqual([p.suffix for p in self._files()], [".mp3"])

    def test_provider_failure_raises_stall_audio_error(self):
        async def _boom(_text: str):
            raise RuntimeError("provider down")

        with _env(self.cache_dir):
            with patch("shared.stall_audio.stream_speech_mp3", new=_boom):
                with self.assertRaises(StallAudioError):
                    asyncio.run(get_stall_audio(STALL_KEY_MAIL_CHECK))
        self.assertEqual(self._files(), [])


if __name__ == "__main__":
    unittest.main()
