"""Streaming chat + streaming TTS + barge-in (WS /chat/stream).

TestClient drives the app through a blocking portal, so true barge-in races are
covered at the unit level against the cancellation token and the send
choke-point; TestClient covers the protocol-level cases. Fully offline: both
Anthropic and ElevenLabs are patched.

Run:  python -m unittest tests.test_streaming_voice_v1 -v
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from routers.chat_stream import (
    MAX_CONNECTIONS_PER_USER,
    OPEN_CONNECTIONS_PER_USER,
    WS_CLOSE_TOO_MANY_CONNECTIONS,
    _http_error_code,
    _persist_tool_round,
    _register_connection,
    _release_connection,
    _TurnVoice,
)
from shared import db
from shared.context_budget import build_model_messages, repair_tool_call_pairs
from shared.elevenlabs_stream import ElevenLabsStreamError, ElevenLabsStreamSession
from shared.local_auth.store import use_memory_store
from shared.profile_schema import empty_profile
from shared.streaming.phrases import SpeechChunker
from shared.streaming.protocol import (
    ERROR_FORBIDDEN_CONVERSATION,
    ERROR_INTERNAL,
    ERROR_INVALID_CONVERSATION,
    ERROR_INVALID_FRAME,
    ERROR_TTS_UNAVAILABLE,
    ERROR_UNSUPPORTED_MODE,
    MAX_FRAME_CHARS,
    MAX_MESSAGE_CHARS,
    ProtocolError,
    error_frame,
    parse_client_frame,
    text_delta_frame,
)
from shared.streaming.turn import StreamTurn, TurnSender, frame_allowed

DEV_USER = "00000000-0000-4000-8000-000000000001"


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "dev",
        "APP_ENV": "test",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "unittest-placeholder",  # pragma: allowlist secret
        "ELEVENLABS_API_KEY": "el-test-key",  # pragma: allowlist secret
        "ELEVENLABS_VOICE_ID": "voice-test-id",
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


# ---------------------------------------------------------------------------
# Fakes — no network anywhere
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self, *_args, **_kwargs):
        return dict(self.__dict__)


def _text_block(text: str) -> _Block:
    return _Block(type="text", text=text)


def _tool_block(block_id: str, name: str, tool_input: dict) -> _Block:
    return _Block(type="tool_use", id=block_id, name=name, input=tool_input)


def _message(stop_reason: str, content=(), *, input_tokens=12, output_tokens=7):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=list(content),
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class _FakeStream:
    """Mimics anthropic 0.42.0 AsyncMessageStream (text_stream + close)."""

    def __init__(self, deltas, message, *, delay: float = 0.0):
        self.deltas = list(deltas)
        self.message = message
        self.delay = delay
        self.closed = False
        self.finalized = False
        self.exited = False

    @property
    def text_stream(self):
        async def _gen():
            try:
                for delta in self.deltas:
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    yield delta
            finally:
                self.finalized = True

        return _gen()

    async def get_final_message(self):
        return self.message

    async def close(self) -> None:
        self.closed = True


class _FakeStreamManager:
    def __init__(self, stream: _FakeStream):
        self._stream = stream

    async def __aenter__(self) -> _FakeStream:
        return self._stream

    async def __aexit__(self, *_exc) -> bool:
        self._stream.exited = True
        return False


class _FakeAnthropic:
    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.streams: list[_FakeStream] = []
        self.calls: list[dict] = []
        self.client_closed = False

    @property
    def messages(self):
        return self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        stream = self._rounds.pop(0)
        self.streams.append(stream)
        return _FakeStreamManager(stream)

    async def close(self) -> None:
        self.client_closed = True


class _FakeTTSSession:
    """Mimics ElevenLabsStreamSession: audio arrives as text is fed."""

    def __init__(self, *, fail_open=False, fail_send=False, audio=b"MP3"):
        self.fail_open = fail_open
        self.fail_send = fail_send
        self.audio = audio
        self.opened = False
        self.closed = False
        self.aborted = False
        self.finished = False
        self.sent: list[str] = []
        self._queue: asyncio.Queue = asyncio.Queue()

    async def open(self) -> None:
        if self.fail_open:
            raise ElevenLabsStreamError("connect failed")
        self.opened = True

    async def send_text(self, text: str) -> None:
        self.sent.append(text)
        if self.fail_send:
            raise ElevenLabsStreamError("send failed")
        await self._queue.put(self.audio)

    async def flush(self, trailing_text: str = "") -> None:
        if trailing_text.strip():
            await self._queue.put(self.audio)

    async def flush_and_finish(self, trailing_text: str = "") -> None:
        self.finished = True
        if trailing_text.strip():
            await self._queue.put(self.audio)
        await self._queue.put(None)

    async def abort(self) -> None:
        self.aborted = True
        self.closed = True
        await self._queue.put(None)

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(None)

    async def audio_chunks(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class _RecordingWebSocket:
    """Minimal WebSocket stand-in for TurnSender unit tests."""

    def __init__(self):
        self.frames: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)


class _Store:
    """In-memory stand-in for the shared.db functions /chat/stream touches."""

    def __init__(self):
        self.conversations: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.pending_actions: list[dict] = []

    async def create_conversation(self, conversation_id, user_id, mode, title=None):
        self.conversations[conversation_id] = {
            "id": conversation_id,
            "user_id": user_id,
            "mode": mode,
            "summary_text": None,
            "summary_through_seq": None,
        }

    async def get_conversation(self, conversation_id):
        return self.conversations.get(conversation_id)

    async def load_messages_with_seq(self, _conversation_id):
        return []

    async def append_message(self, conversation_id, role, content):
        self.messages.append(
            {"conversation_id": conversation_id, "role": role, "content": content}
        )
        return len(self.messages)

    async def set_conversation_title_if_empty(self, _conversation_id, _title):
        return None

    async def insert_turn_metrics(self, *_args, **_kwargs):
        return None

    async def update_conversation_summary(self, *_args, **_kwargs):
        return None

    async def find_conversations_for_open(self, *_args, **_kwargs):
        return []

    async def get_active_prompt_overrides(self, *_args, **_kwargs):
        return []

    async def create_pending_action(self, **kwargs):
        self.pending_actions.append(kwargs)
        return "act_stream_1"

    def assistant_texts(self) -> list[str]:
        return [
            m["content"]
            for m in self.messages
            if m["role"] == "assistant" and isinstance(m["content"], str)
        ]


def _ensure_stream_route(app) -> None:
    """The coordinator wires this router into main.py; tolerate either state."""
    if any(getattr(route, "path", None) == "/chat/stream" for route in app.routes):
        return
    from routers.chat_stream import router as chat_stream_router

    app.include_router(chat_stream_router)


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll a server-side condition from the synchronous test thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _read_until(websocket, frame_type: str, *, limit: int = 400) -> list[dict]:
    """Collect frames up to and including the first `frame_type`."""
    frames: list[dict] = []
    for _ in range(limit):
        frame = websocket.receive_json()
        frames.append(frame)
        if frame["type"] == frame_type:
            return frames
    raise AssertionError(f"never received {frame_type}: {[f['type'] for f in frames]}")


# ---------------------------------------------------------------------------
# Unit: cancellation token + send choke-point (the barge-in rules)
# ---------------------------------------------------------------------------

class StaleTurnGuardTests(unittest.TestCase):
    def test_active_turn_frames_pass(self):
        turn = StreamTurn("turn-a")
        self.assertTrue(
            frame_allowed(frame_type="text_delta", turn=turn, active_turn_id="turn-a")
        )

    def test_superseded_turn_frames_dropped(self):
        turn = StreamTurn("turn-a")
        self.assertFalse(
            frame_allowed(frame_type="text_delta", turn=turn, active_turn_id="turn-b")
        )
        self.assertFalse(
            frame_allowed(frame_type="audio_chunk", turn=turn, active_turn_id=None)
        )

    def test_cancelled_turn_may_only_emit_turn_cancelled(self):
        turn = StreamTurn("turn-a")
        turn.cancel()
        self.assertFalse(
            frame_allowed(frame_type="text_delta", turn=turn, active_turn_id="turn-a")
        )
        self.assertTrue(
            frame_allowed(
                frame_type="turn_cancelled", turn=turn, active_turn_id="turn-a"
            )
        )

    def test_connection_level_frames_always_pass(self):
        self.assertTrue(
            frame_allowed(frame_type="error", turn=None, active_turn_id=None)
        )


class TurnSenderTests(unittest.TestCase):
    def test_audio_sequence_is_strictly_increasing_and_gap_free(self):
        async def _run():
            ws = _RecordingWebSocket()
            sender = TurnSender(ws)
            turn = StreamTurn("turn-a")
            sender.set_active_turn(turn)
            for _ in range(5):
                await sender.send_audio(turn, kind="assistant", audio=b"MP3")
            await sender.send_audio(turn, kind="stall", audio=b"MP3")
            return ws.frames

        frames = asyncio.run(_run())
        sequences = [f["sequence"] for f in frames]
        self.assertEqual(sequences, [0, 1, 2, 3, 4, 5])
        self.assertEqual(frames[-1]["kind"], "stall")
        self.assertEqual(frames[-1]["mime_type"], "audio/mpeg")

    def test_concurrent_producers_never_emit_out_of_order(self):
        async def _run():
            ws = _RecordingWebSocket()
            sender = TurnSender(ws)
            turn = StreamTurn("turn-a")
            sender.set_active_turn(turn)

            async def producer(kind):
                for _ in range(20):
                    await sender.send_audio(turn, kind=kind, audio=b"MP3")
                    await asyncio.sleep(0)

            await asyncio.gather(producer("assistant"), producer("stall"))
            return ws.frames

        frames = asyncio.run(_run())
        sequences = [f["sequence"] for f in frames]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(sequences, list(range(40)))

    def test_stale_callback_cannot_emit_into_a_newer_turn(self):
        async def _run():
            ws = _RecordingWebSocket()
            sender = TurnSender(ws)
            old = StreamTurn("turn-old")
            sender.set_active_turn(old)
            await sender.send(text_delta_frame(turn_id=old.turn_id, delta="hi"), turn=old)

            old.cancel()
            await sender.send_cancellation(old)
            sender.clear_active_turn(old)

            new = StreamTurn("turn-new")
            sender.set_active_turn(new)

            # Late callbacks from the cancelled turn arrive after the new one started.
            await sender.send(
                text_delta_frame(turn_id=old.turn_id, delta="leak"), turn=old
            )
            await sender.send_audio(old, kind="assistant", audio=b"MP3")
            await sender.send_cancellation(old)
            await sender.send(
                text_delta_frame(turn_id=new.turn_id, delta="fresh"), turn=new
            )
            return ws.frames

        frames = asyncio.run(_run())
        self.assertEqual(
            [(f["type"], f["turn_id"]) for f in frames],
            [
                ("text_delta", "turn-old"),
                ("turn_cancelled", "turn-old"),
                ("text_delta", "turn-new"),
            ],
        )
        self.assertEqual(sum(1 for f in frames if f["type"] == "turn_cancelled"), 1)

    def test_dead_socket_marks_sender_closed(self):
        class _Broken(_RecordingWebSocket):
            async def send_json(self, frame):
                raise OSError("socket gone")

        async def _run():
            sender = TurnSender(_Broken())
            turn = StreamTurn("turn-a")
            sender.set_active_turn(turn)
            first = await sender.send(
                text_delta_frame(turn_id=turn.turn_id, delta="x"), turn=turn
            )
            return first, sender.closed

        sent, closed = asyncio.run(_run())
        self.assertFalse(sent)
        self.assertTrue(closed)


class ProtocolParsingTests(unittest.TestCase):
    def test_user_turn_defaults_voice_on(self):
        frame = parse_client_frame(
            '{"type":"user_turn","mode":"fitness","message":"hi"}'
        )
        self.assertEqual(frame.mode, "fitness")
        self.assertTrue(frame.voice.enabled)
        self.assertIsNone(frame.conversation_id)

    def test_bad_json_and_unknown_type_rejected(self):
        for raw in ("not json", '{"type":"nope"}', "[]", None):
            with self.assertRaises(ProtocolError):
                parse_client_frame(raw)

    def test_empty_message_rejected(self):
        with self.assertRaises(ProtocolError):
            parse_client_frame('{"type":"user_turn","message":""}')

    def test_oversize_message_rejected(self):
        huge = "x" * 20_000
        with self.assertRaises(ProtocolError):
            parse_client_frame('{"type":"user_turn","message":"%s"}' % huge)

    def test_error_message_never_echoes_user_text(self):
        try:
            parse_client_frame('{"type":"user_turn","message":123456789}')
        except ProtocolError as exc:
            self.assertNotIn("123456789", exc.message)
        else:
            self.fail("expected ProtocolError")

    def test_unknown_error_code_is_rejected_at_build_time(self):
        with self.assertRaises(ValueError):
            error_frame(turn_id=None, code="made_up", message="x")

    def test_oversize_raw_frame_is_rejected_before_json_parsing(self):
        huge = '{"type":"user_turn","message":"' + "z" * 200_000 + '"}'
        self.assertGreater(len(huge), MAX_FRAME_CHARS)
        with patch(
            "shared.streaming.protocol.json.loads",
            side_effect=AssertionError("oversize frame must not be parsed"),
        ):
            with self.assertRaises(ProtocolError) as ctx:
                parse_client_frame(huge)
        self.assertEqual(ctx.exception.code, ERROR_INVALID_FRAME)
        self.assertNotIn("zzzz", ctx.exception.message)

    def test_oversize_raw_bytes_frame_is_rejected_before_decoding(self):
        with self.assertRaises(ProtocolError):
            parse_client_frame(b"{" + b"z" * (MAX_FRAME_CHARS + 1))

    def test_max_length_message_still_parses(self):
        raw = json.dumps(
            {"type": "user_turn", "message": "y" * MAX_MESSAGE_CHARS}
        )
        self.assertLessEqual(len(raw), MAX_FRAME_CHARS)
        frame = parse_client_frame(raw)
        self.assertEqual(len(frame.message), MAX_MESSAGE_CHARS)


class ErrorCodeMappingTests(unittest.TestCase):
    def test_conversation_failures_keep_their_specific_codes(self):
        self.assertEqual(_http_error_code(400), ERROR_INVALID_CONVERSATION)
        self.assertEqual(_http_error_code(403), ERROR_FORBIDDEN_CONVERSATION)

    def test_503_is_internal_not_a_recoverable_voice_failure(self):
        # tts_unavailable promises the client a response_complete still follows
        # (contract §8). A 503 from the turn machinery is a dead turn, so
        # labelling it that way would hang the client forever.
        self.assertEqual(_http_error_code(503), ERROR_INTERNAL)
        self.assertNotEqual(_http_error_code(503), ERROR_TTS_UNAVAILABLE)

    def test_unmapped_status_codes_are_internal(self):
        for status in (404, 500, 502, 504):
            self.assertEqual(_http_error_code(status), ERROR_INTERNAL)


class ContextToolPairRepairTests(unittest.TestCase):
    """A half-written tool round must not poison a conversation forever.

    Anthropic rejects the whole conversation when history holds a `tool_use`
    with no `tool_result` (or the reverse), so build_model_messages repairs it
    on read for both the streamed and the REST path.
    """

    @staticmethod
    def _built(recent: list[dict]):
        return build_model_messages(
            system_prompt="sys",
            profile_block="",
            summary_text=None,
            summary_through_seq=None,
            recent_messages=recent,
            current_user_message={"role": "user", "content": "and now?"},
        )

    @staticmethod
    def _blocks(messages: list[dict]) -> list[dict]:
        return [
            block
            for message in messages
            for block in (
                message["content"] if isinstance(message["content"], list) else []
            )
        ]

    def test_dangling_tool_use_is_dropped_but_its_text_survives(self):
        built = self._built(
            [
                {"role": "user", "content": "What's on my calendar?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "list_calendar_events",
                            "input": {},
                        },
                    ],
                },
            ]
        )
        kinds = [block["type"] for block in self._blocks(built.messages)]
        self.assertEqual(kinds, ["text"])
        self.assertIn("Let me check.", json.dumps(built.messages))

    def test_assistant_message_of_only_unmatched_tool_use_is_dropped(self):
        built = self._built(
            [
                {"role": "user", "content": "Log my oatmeal."},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_9",
                            "name": "create_pending_action",
                            "input": {},
                        }
                    ],
                },
            ]
        )
        self.assertNotIn("tu_9", json.dumps(built.messages))
        self.assertEqual([m["role"] for m in built.messages], ["user", "user"])
        self.assertEqual(built.raw_messages_included, 2)

    def test_orphan_tool_result_is_dropped(self):
        built = self._built(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_missing",
                            "content": "2 events today.",
                        }
                    ],
                },
                {"role": "assistant", "content": "You have two meetings."},
            ]
        )
        self.assertNotIn("tool_result", json.dumps(built.messages))
        self.assertIn("You have two meetings.", json.dumps(built.messages))

    def test_matched_tool_round_is_passed_through_verbatim(self):
        pair = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "list_calendar_events",
                        "input": {"range": "today"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "2 events today.",
                    }
                ],
            },
        ]
        built = self._built([dict(m) for m in pair])
        self.assertEqual(built.messages[:2], pair)

    def test_partially_matched_assistant_message_keeps_the_matched_call(self):
        built = self._built(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu_ok", "name": "a", "input": {}},
                        {"type": "tool_use", "id": "tu_lost", "name": "b", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_ok",
                            "content": "done",
                        }
                    ],
                },
            ]
        )
        blob = json.dumps(built.messages)
        self.assertIn("tu_ok", blob)
        self.assertNotIn("tu_lost", blob)

    def test_trimming_away_a_tool_use_also_drops_its_result(self):
        built = self._built(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "search",
                            "input": {"query": "x" * 400_000},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": "ok",
                        }
                    ],
                },
            ]
        )
        # The oversized tool_use is trimmed for budget; its result must not be
        # left behind on its own.
        self.assertNotIn("tu_1", json.dumps(built.messages))
        self.assertEqual(built.raw_messages_included, 1)

    def test_repair_is_a_no_op_for_plain_text_history(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        self.assertEqual(repair_tool_call_pairs(history), history)


class ToolRoundPersistenceTests(unittest.TestCase):
    """The two history writes of a tool round are one unit or the conversation
    is permanently broken for every later model call."""

    def test_write_pair_survives_a_hard_cancel_between_the_appends(self):
        async def _run():
            store = _Store()
            assistant_written = asyncio.Event()
            release_results = asyncio.Event()

            async def append(conversation_id, role, content):
                if role == "user":
                    # The hard cancel lands here — between the two appends.
                    await release_results.wait()
                seq = await store.append_message(conversation_id, role, content)
                if role == "assistant":
                    assistant_written.set()
                return seq

            with patch("shared.db.append_message", new=append):
                task = asyncio.create_task(
                    _persist_tool_round(
                        "conv-1",
                        assistant_blocks=[
                            {"type": "tool_use", "id": "tu_1", "name": "t", "input": {}}
                        ],
                        tool_results=[
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_1",
                                "content": "ok",
                            }
                        ],
                    )
                )
                await assistant_written.wait()
                task.cancel()
                release_results.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return store.messages

        messages = asyncio.run(_run())
        self.assertEqual([m["role"] for m in messages], ["assistant", "user"])
        self.assertEqual(
            messages[1]["content"][0]["tool_use_id"],
            messages[0]["content"][0]["id"],
            "a cancelled tool round must never persist tool_use without its result",
        )


class TurnVoiceStartTests(unittest.TestCase):
    def test_hard_cancel_inside_open_closes_the_upstream_socket(self):
        class _HangingOpen(_FakeTTSSession):
            """Connects, then blocks where open() sends its init message."""

            def __init__(self):
                super().__init__()
                self.open_entered = asyncio.Event()

            async def open(self) -> None:
                self.opened = True
                self.open_entered.set()
                await asyncio.sleep(3600)

        async def _run():
            session = _HangingOpen()
            turn = StreamTurn("turn-a")
            sender = TurnSender(_RecordingWebSocket())
            sender.set_active_turn(turn)
            voice = _TurnVoice(turn=turn, sender=sender, requested=True)
            with patch(
                "routers.chat_stream._new_tts_session", new=Mock(return_value=session)
            ):
                task = asyncio.create_task(voice.start())
                await session.open_entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return session, voice

        session, voice = asyncio.run(_run())
        self.assertTrue(
            session.closed, "an abandoned open() must not leak the upstream socket"
        )
        self.assertFalse(voice.streaming)

    def test_cooperative_cancel_during_open_also_aborts_the_socket(self):
        class _HangingOpen(_FakeTTSSession):
            def __init__(self):
                super().__init__()
                self.open_entered = asyncio.Event()

            async def open(self) -> None:
                self.opened = True
                self.open_entered.set()
                await asyncio.sleep(3600)

        async def _run():
            session = _HangingOpen()
            turn = StreamTurn("turn-a")
            sender = TurnSender(_RecordingWebSocket())
            sender.set_active_turn(turn)
            voice = _TurnVoice(turn=turn, sender=sender, requested=True)
            with patch(
                "routers.chat_stream._new_tts_session", new=Mock(return_value=session)
            ):
                task = asyncio.create_task(voice.start())
                await session.open_entered.wait()
                # Barge-in path: the turn aborts the TTS socket it was told about.
                turn.cancel()
                await turn.abort_tts()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return session

        session = asyncio.run(_run())
        self.assertTrue(session.aborted, "barge-in must reach a session still opening")


class SpeechChunkerTests(unittest.TestCase):
    def test_chunks_end_with_space_and_preserve_order(self):
        chunker = SpeechChunker()
        emitted: list[str] = []
        for delta in ("Progressive ", "overload ", "means ", "adding ", "load ", "weekly."):
            emitted.extend(chunker.push(delta))
        emitted.append(chunker.drain())
        joined = "".join(emitted)
        self.assertEqual(joined.strip(), "Progressive overload means adding load weekly.")
        for chunk in emitted:
            self.assertTrue(chunk.endswith(" "), chunk)


# ---------------------------------------------------------------------------
# Unit: ElevenLabs stream-input protocol (no network)
# ---------------------------------------------------------------------------

class _FakeUpstream:
    """Stands in for a websockets client connection."""

    def __init__(self, frames=()):
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        async def _gen():
            for frame in self.frames:
                if self.closed:
                    return
                yield frame

        return _gen()


class ElevenLabsStreamSessionTests(unittest.TestCase):
    def setUp(self):
        self.connect_calls: list[tuple] = []

    def _session(self, upstream: _FakeUpstream, **kwargs) -> ElevenLabsStreamSession:
        async def _connect(url, **connect_kwargs):
            self.connect_calls.append((url, connect_kwargs))
            return upstream

        return ElevenLabsStreamSession(
            voice_id="voice-1",
            model_id="eleven_flash_v2_5",
            api_key="el-test-key",  # pragma: allowlist secret
            connect=_connect,
            **kwargs,
        )

    def test_handshake_header_auth_and_init_message(self):
        upstream = _FakeUpstream()

        async def _run():
            async with self._session(upstream) as session:
                await session.send_text("Hello there")
                await session.flush_and_finish("tail")

        asyncio.run(_run())

        url, kwargs = self.connect_calls[0]
        self.assertIn("/v1/text-to-speech/voice-1/stream-input", url)
        self.assertIn("model_id=eleven_flash_v2_5", url)
        self.assertIn("output_format=mp3_44100_128", url)
        self.assertIn("inactivity_timeout=180", url)
        self.assertEqual(kwargs["additional_headers"], {"xi-api-key": "el-test-key"})
        self.assertNotIn("xi_api_key", url)

        init = upstream.sent[0]
        self.assertEqual(init["text"], " ")
        self.assertEqual(
            init["generation_config"]["chunk_length_schedule"], [120, 160, 250, 290]
        )
        self.assertNotIn("xi_api_key", init)
        self.assertEqual(upstream.sent[1], {"text": "Hello there "})
        self.assertEqual(upstream.sent[2], {"text": "tail ", "flush": True})
        self.assertEqual(upstream.sent[3], {"text": ""})
        self.assertTrue(upstream.closed, "context manager must close the socket")

    def test_short_reply_is_flushed_at_end_of_turn(self):
        upstream = _FakeUpstream()

        async def _run():
            async with self._session(upstream) as session:
                await session.send_text("Hi")
                await session.flush_and_finish()

        asyncio.run(_run())
        self.assertTrue(any(msg.get("flush") for msg in upstream.sent))

    def test_audio_frames_decode_and_stop_on_final(self):
        for final_key in ("isFinal", "is_final"):
            with self.subTest(final_key=final_key):
                upstream = _FakeUpstream(
                    [
                        json.dumps({"audio": base64.b64encode(b"one").decode()}),
                        json.dumps({"audio": base64.b64encode(b"two").decode()}),
                        json.dumps({final_key: True}),
                        json.dumps({"audio": base64.b64encode(b"never").decode()}),
                    ]
                )

                async def _run():
                    session = self._session(upstream)
                    await session.open()
                    try:
                        return [chunk async for chunk in session.audio_chunks()]
                    finally:
                        await session.close()

                self.assertEqual(asyncio.run(_run()), [b"one", b"two"])
                self.assertTrue(upstream.closed)

    def test_abort_closes_socket_and_stops_iteration(self):
        upstream = _FakeUpstream(
            [json.dumps({"audio": base64.b64encode(b"one").decode()})] * 5
        )

        async def _run():
            session = self._session(upstream)
            await session.open()
            collected = []
            async for chunk in session.audio_chunks():
                collected.append(chunk)
                await session.abort()
            return collected, session

        collected, session = asyncio.run(_run())
        self.assertEqual(collected, [b"one"])
        self.assertTrue(session.aborted)
        self.assertTrue(upstream.closed, "abort must close the upstream socket")

    def test_send_after_abort_is_a_noop(self):
        upstream = _FakeUpstream()

        async def _run():
            session = self._session(upstream)
            await session.open()
            await session.abort()
            await session.send_text("ignored")
            await session.flush_and_finish("ignored")
            return upstream.sent

        sent = asyncio.run(_run())
        self.assertEqual([msg.get("text") for msg in sent], [" "])

    def test_connect_failure_raises_and_leaves_no_socket(self):
        async def _connect(*_args, **_kwargs):
            raise OSError("no route to host")

        async def _run():
            session = ElevenLabsStreamSession(
                voice_id="voice-1",
                model_id="m",
                api_key="k",  # pragma: allowlist secret
                connect=_connect,
            )
            with self.assertRaises(ElevenLabsStreamError):
                await session.open()
            await session.close()
            return session

        session = asyncio.run(_run())
        self.assertTrue(session.closed)

    def test_unconfigured_provider_raises_503(self):
        async def _connect(*_args, **_kwargs):  # pragma: no cover - never reached
            raise AssertionError("must not connect without configuration")

        async def _run():
            session = ElevenLabsStreamSession(connect=_connect)
            with self.assertRaises(HTTPException) as ctx:
                await session.open()
            return ctx.exception

        with patch.dict(
            os.environ,
            {"ELEVENLABS_API_KEY": "", "ELEVENLABS_VOICE_ID": ""},
            clear=False,
        ):
            exc = asyncio.run(_run())
        self.assertEqual(exc.status_code, 503)


# ---------------------------------------------------------------------------
# Protocol: WS /chat/stream end to end
# ---------------------------------------------------------------------------

class StreamingChatRouteTests(unittest.TestCase):
    def setUp(self):
        use_memory_store(True)
        self.addCleanup(lambda: use_memory_store(False))
        self.store = _Store()
        self.patches = [
            patch("shared.db.init_pool", new_callable=AsyncMock),
            patch("shared.db.close_pool", new_callable=AsyncMock),
            patch("shared.db.create_conversation", new=self.store.create_conversation),
            patch("shared.db.get_conversation", new=self.store.get_conversation),
            patch(
                "shared.db.load_messages_with_seq", new=self.store.load_messages_with_seq
            ),
            patch("shared.db.append_message", new=self.store.append_message),
            patch(
                "shared.db.set_conversation_title_if_empty",
                new=self.store.set_conversation_title_if_empty,
            ),
            patch("shared.db.insert_turn_metrics", new=self.store.insert_turn_metrics),
            patch(
                "shared.db.update_conversation_summary",
                new=self.store.update_conversation_summary,
            ),
            patch(
                "shared.db.find_conversations_for_open",
                new=self.store.find_conversations_for_open,
            ),
            patch(
                "shared.db.get_active_prompt_overrides",
                new=self.store.get_active_prompt_overrides,
            ),
            patch(
                "shared.db.create_pending_action", new=self.store.create_pending_action
            ),
            patch(
                "main.get_profile",
                new_callable=AsyncMock,
                return_value=empty_profile(DEV_USER),
            ),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        OPEN_CONNECTIONS_PER_USER.clear()
        self.addCleanup(OPEN_CONNECTIONS_PER_USER.clear)

    def _client(self) -> TestClient:
        from main import app

        _ensure_stream_route(app)
        return TestClient(app)

    def _anthropic(self, rounds):
        return patch(
            "routers.chat_stream._anthropic_client",
            new=Mock(return_value=_FakeAnthropic(rounds)),
        )

    def _tts(self, session):
        return patch(
            "routers.chat_stream._new_tts_session", new=Mock(return_value=session)
        )

    # -- auth ---------------------------------------------------------------

    def test_unauthorized_connect_is_denied_with_401(self):
        with _env(AUTH_MODE="self"):
            with self._client() as client:
                with self.assertRaises(WebSocketDenialResponse) as ctx:
                    with client.websocket_connect("/chat/stream"):
                        pass
        self.assertEqual(ctx.exception.status_code, 401)

    # -- happy path ---------------------------------------------------------

    def test_turn_completes_with_ordered_text_deltas(self):
        stream = _FakeStream(
            ["Progressive ", "overload ", "means adding load."],
            _message("end_turn"),
        )
        with _env():
            with self._anthropic([stream]):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "What is progressive overload?",
                                "conversation_id": None,
                                "voice": {"enabled": False},
                            }
                        )
                        frames = _read_until(ws, "response_complete")

        self.assertEqual(frames[0]["type"], "turn_started")
        turn_id = frames[0]["turn_id"]
        conversation_id = frames[0]["conversation_id"]
        deltas = [f["delta"] for f in frames if f["type"] == "text_delta"]
        self.assertEqual(deltas, ["Progressive ", "overload ", "means adding load."])
        self.assertTrue(all(f["turn_id"] == turn_id for f in frames))

        complete = frames[-1]
        self.assertEqual(complete["reply"], "Progressive overload means adding load.")
        self.assertEqual(complete["conversation_id"], conversation_id)
        self.assertIsNone(complete["pending_action"])
        self.assertIsNone(complete["visual_panel"])
        self.assertIsNone(complete["research"])
        self.assertEqual(complete["client_actions"], [])
        self.assertIn(
            "Progressive overload means adding load.", self.store.assistant_texts()
        )

    def test_navigate_command_streams_client_action(self):
        with _env():
            with self._client() as client:
                with client.websocket_connect("/chat/stream") as ws:
                    ws.send_json(
                        {
                            "type": "user_turn",
                            "mode": "diet",
                            "message": "Open fitness.",
                            "voice": {"enabled": False},
                        }
                    )
                    frames = _read_until(ws, "response_complete")
        complete = frames[-1]
        self.assertEqual(complete["reply"], "Opening Fitness.")
        self.assertEqual(
            complete["client_actions"], [{"type": "navigate", "target": "fitness"}]
        )

    def test_unsupported_mode_error_frame(self):
        with _env():
            with self._client() as client:
                with client.websocket_connect("/chat/stream") as ws:
                    ws.send_json(
                        {"type": "user_turn", "mode": "health", "message": "hi"}
                    )
                    frame = ws.receive_json()
        self.assertEqual(frame["type"], "error")
        self.assertEqual(frame["code"], ERROR_UNSUPPORTED_MODE)
        self.assertIsNone(frame["turn_id"])

    def test_non_uuid_conversation_id_errors_before_turn_started(self):
        stream = _FakeStream(["Fresh conversation."], _message("end_turn"))
        with _env():
            with self._anthropic([stream]):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Where were we?",
                                "conversation_id": "not-a-uuid",
                                "voice": {"enabled": False},
                            }
                        )
                        rejected = ws.receive_json()
                        # The socket stays usable: retry with null works.
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Start over then.",
                                "conversation_id": None,
                                "voice": {"enabled": False},
                            }
                        )
                        after = _read_until(ws, "response_complete")

        self.assertEqual(rejected["type"], "error")
        self.assertEqual(rejected["code"], ERROR_INVALID_CONVERSATION)
        self.assertIsNone(
            rejected["turn_id"],
            "an error before turn_started must not carry an unknown turn_id",
        )
        self.assertEqual(after[0]["type"], "turn_started")
        self.assertEqual(after[-1]["reply"], "Fresh conversation.")

    def test_another_users_conversation_is_forbidden_before_turn_started(self):
        other_conversation = "11111111-1111-4111-8111-111111111111"
        self.store.conversations[other_conversation] = {
            "id": other_conversation,
            "user_id": "00000000-0000-4000-8000-0000000000aa",
            "mode": "fitness",
            "summary_text": None,
            "summary_through_seq": None,
        }
        with _env():
            with self._client() as client:
                with client.websocket_connect("/chat/stream") as ws:
                    ws.send_json(
                        {
                            "type": "user_turn",
                            "mode": "fitness",
                            "message": "Read me that conversation.",
                            "conversation_id": other_conversation,
                            "voice": {"enabled": False},
                        }
                    )
                    frame = ws.receive_json()

        self.assertEqual(frame["type"], "error")
        self.assertEqual(frame["code"], ERROR_FORBIDDEN_CONVERSATION)
        self.assertIsNone(frame["turn_id"])
        self.assertEqual(
            self.store.messages, [], "a forbidden turn must write nothing at all"
        )

    def test_database_blip_during_setup_errors_with_a_null_turn_id(self):
        # A Postgres hiccup on conversation creation is the routine failure
        # here. It must not hand the client a turn_id it never saw announced.
        with _env():
            with patch(
                "shared.db.create_conversation",
                new_callable=AsyncMock,
                side_effect=db.DatabaseUnavailableError(
                    "Database temporarily unavailable."
                ),
            ):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "How am I doing?",
                                "voice": {"enabled": False},
                            }
                        )
                        frame = ws.receive_json()

        self.assertEqual(frame["type"], "error")
        self.assertEqual(frame["code"], ERROR_INTERNAL)
        self.assertIsNone(
            frame["turn_id"],
            "an error before turn_started must not introduce an unknown turn_id",
        )

    def test_service_unavailable_mid_turn_is_reported_as_internal_error(self):
        stream = _FakeStream(["never streamed"], _message("end_turn"))
        with _env():
            with self._anthropic([stream]), patch(
                "main.get_profile",
                new_callable=AsyncMock,
                side_effect=HTTPException(
                    status_code=503, detail="Dependency temporarily unavailable."
                ),
            ):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "How am I doing?",
                                "voice": {"enabled": False},
                            }
                        )
                        frames = _read_until(ws, "error")

        self.assertEqual(frames[0]["type"], "turn_started")
        failure = frames[-1]
        self.assertEqual(
            failure["code"],
            ERROR_INTERNAL,
            "a dead turn must not be labelled with a non-fatal voice code",
        )
        self.assertNotEqual(failure["code"], ERROR_TTS_UNAVAILABLE)
        self.assertEqual(failure["turn_id"], frames[0]["turn_id"])
        self.assertNotIn("response_complete", [f["type"] for f in frames])

    def test_malformed_frame_error_does_not_close_socket(self):
        stream = _FakeStream(["Fine."], _message("end_turn"))
        with _env():
            with self._anthropic([stream]):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_text("{not json")
                        first = ws.receive_json()
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "still there?",
                                "voice": {"enabled": False},
                            }
                        )
                        frames = _read_until(ws, "response_complete")
        self.assertEqual(first["type"], "error")
        self.assertEqual(first["code"], ERROR_INVALID_FRAME)
        self.assertEqual(frames[-1]["reply"], "Fine.")

    # -- audio --------------------------------------------------------------

    def test_audio_chunk_sequences_are_strictly_increasing(self):
        stream = _FakeStream(
            ["The first sentence is long enough to flush. ", "And a second one here."],
            _message("end_turn"),
        )
        session = _FakeTTSSession()
        with _env():
            with self._anthropic([stream]), self._tts(session):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Talk to me.",
                                "voice": {"enabled": True},
                            }
                        )
                        frames = _read_until(ws, "response_complete")

        audio = [f for f in frames if f["type"] == "audio_chunk"]
        self.assertGreaterEqual(len(audio), 1)
        sequences = [f["sequence"] for f in audio]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(set(sequences)), len(sequences))
        self.assertEqual(sequences[0], 0)
        for frame in audio:
            self.assertEqual(frame["kind"], "assistant")
            self.assertEqual(frame["mime_type"], "audio/mpeg")
            self.assertTrue(frame["data_base64"])
        # response_complete is the terminal frame — all audio precedes it.
        self.assertEqual(frames[-1]["type"], "response_complete")
        self.assertTrue(session.finished)

    def test_tts_failure_still_completes_the_text_turn(self):
        stream = _FakeStream(["Text still works."], _message("end_turn"))
        session = _FakeTTSSession(fail_open=True)
        with _env():
            with self._anthropic([stream]), self._tts(session):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Say something.",
                                "voice": {"enabled": True},
                            }
                        )
                        frames = _read_until(ws, "response_complete")

        errors = [f for f in frames if f["type"] == "error"]
        self.assertEqual([e["code"] for e in errors], [ERROR_TTS_UNAVAILABLE])
        self.assertEqual(frames[-1]["reply"], "Text still works.")
        self.assertEqual([f for f in frames if f["type"] == "audio_chunk"], [])

    def test_stall_audio_matches_the_tool_actually_invoked(self):
        rounds = [
            _FakeStream(
                [],
                _message(
                    "tool_use",
                    [_tool_block("tu_1", "list_calendar_events", {"range": "today"})],
                ),
            ),
            _FakeStream(["You have two meetings."], _message("end_turn")),
        ]
        session = _FakeTTSSession()
        stall = AsyncMock(return_value=b"STALL-MP3")
        with _env():
            with self._anthropic(rounds), self._tts(session), patch(
                "routers.chat_stream.get_stall_audio", new=stall
            ), patch(
                "main._run_tool",
                new_callable=AsyncMock,
                return_value=("2 events today.", None, None, []),
            ):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "mail_calendar",
                                "message": "What's on my calendar?",
                                "voice": {"enabled": True},
                            }
                        )
                        frames = _read_until(ws, "response_complete")

        stall.assert_awaited_once_with("calendar_check")
        stall_frames = [
            f for f in frames if f["type"] == "audio_chunk" and f["kind"] == "stall"
        ]
        self.assertEqual(len(stall_frames), 1)
        self.assertEqual(stall_frames[0]["sequence"], 0)
        self.assertEqual(frames[-1]["reply"], "You have two meetings.")

    # -- Confirm Gate -------------------------------------------------------

    def test_pending_action_surfaces_and_nothing_executes(self):
        rounds = [
            _FakeStream(
                [],
                _message(
                    "tool_use",
                    [
                        _tool_block(
                            "tu_1",
                            "create_pending_action",
                            {
                                "description": "Save 420 calories of oatmeal.",
                                "action_type": "save_food_entry",
                                "payload": {"calories": 420},
                            },
                        )
                    ],
                ),
            ),
            _FakeStream(["Say the word and I'll save it."], _message("end_turn")),
        ]
        execute = AsyncMock()
        resolve = AsyncMock()
        with _env():
            with self._anthropic(rounds), patch(
                "shared.db.insert_food_entry", new=execute
            ), patch("shared.db.resolve_pending_action", new=resolve):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "diet",
                                "message": "Log my oatmeal.",
                                "voice": {"enabled": False},
                            }
                        )
                        frames = _read_until(ws, "response_complete")

        complete = frames[-1]
        self.assertEqual(
            complete["pending_action"],
            {
                "action_id": "act_stream_1",
                "description": "Save 420 calories of oatmeal.",
            },
        )
        self.assertEqual(len(self.store.pending_actions), 1)
        execute.assert_not_awaited()
        resolve.assert_not_awaited()

    # -- barge-in -----------------------------------------------------------

    def _slow_stream(self, count: int = 80) -> _FakeStream:
        return _FakeStream(
            [f"word{i} " for i in range(count)], _message("end_turn"), delay=0.02
        )

    def test_interrupt_cancels_the_turn_and_emits_turn_cancelled(self):
        slow = self._slow_stream()
        session = _FakeTTSSession()
        with _env():
            with self._anthropic([slow]), self._tts(session):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Tell me a long story.",
                                "voice": {"enabled": True},
                            }
                        )
                        started = ws.receive_json()
                        turn_id = started["turn_id"]
                        ws.receive_json()  # at least one streamed frame
                        ws.send_json({"type": "interrupt", "turn_id": turn_id})
                        frames = _read_until(ws, "turn_cancelled")
                        # turn_cancelled is acked immediately; the generation
                        # task unwinds right behind it.
                        unwound = _wait_until(
                            lambda: slow.closed
                            and any(
                                "Interrupted" in text
                                for text in self.store.assistant_texts()
                            )
                        )

        self.assertEqual(started["type"], "turn_started")
        self.assertEqual(frames[-1]["turn_id"], turn_id)
        self.assertEqual(sum(1 for f in frames if f["type"] == "turn_cancelled"), 1)
        self.assertNotIn("response_complete", [f["type"] for f in frames])
        self.assertTrue(unwound, "cancelled turn must unwind after the ack")
        self.assertTrue(slow.closed, "Claude stream must be closed explicitly")
        self.assertTrue(session.aborted or session.closed)
        # Partial speech is preserved, marked as interrupted — never as complete.
        partials = [t for t in self.store.assistant_texts() if "Interrupted" in t]
        self.assertEqual(len(partials), 1)
        self.assertTrue(partials[0].startswith("word0"))

    def _tool_pairing(self) -> tuple[set[str], set[str]]:
        """(tool_use ids, tool_result ids) across everything persisted."""
        uses: set[str] = set()
        results: set[str] = set()
        for message in self.store.messages:
            content = message["content"]
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    uses.add(block["id"])
                elif block.get("type") == "tool_result":
                    results.add(block["tool_use_id"])
        return uses, results

    def _read_until_delta_startswith(self, ws, prefix: str, *, limit: int = 400) -> None:
        for _ in range(limit):
            frame = ws.receive_json()
            if frame["type"] == "text_delta" and frame["delta"].startswith(prefix):
                return
        raise AssertionError(f"never saw a text_delta starting with {prefix!r}")

    def test_interrupt_during_a_tool_round_leaves_no_dangling_tool_use(self):
        rounds = [
            _FakeStream(
                ["Checking now. "],
                _message(
                    "tool_use",
                    [
                        _text_block("Checking now."),
                        _tool_block("tu_1", "list_calendar_events", {"range": "today"}),
                    ],
                ),
            ),
            _FakeStream(["You have two meetings."], _message("end_turn")),
        ]
        state = {"in_tool": False}

        async def slow_tool(*_args, **_kwargs):
            state["in_tool"] = True
            try:
                await asyncio.sleep(0.6)
            finally:
                state["in_tool"] = False
            return ("2 events today.", None, None, [])

        with _env():
            with self._anthropic(rounds), patch("main._run_tool", new=slow_tool):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "mail_calendar",
                                "message": "What's on my calendar?",
                                "voice": {"enabled": False},
                            }
                        )
                        started = ws.receive_json()
                        self.assertTrue(
                            _wait_until(lambda: state["in_tool"]),
                            "the tool must be in flight before the interrupt",
                        )
                        ws.send_json(
                            {"type": "interrupt", "turn_id": started["turn_id"]}
                        )
                        frames = _read_until(ws, "turn_cancelled")
                        settled = _wait_until(lambda: not state["in_tool"])

        self.assertTrue(settled)
        self.assertEqual(sum(1 for f in frames if f["type"] == "turn_cancelled"), 1)
        uses, results = self._tool_pairing()
        self.assertEqual(
            uses,
            {"tu_1"},
            "the tool round under test must actually have been persisted",
        )
        self.assertEqual(
            uses,
            results,
            "an interrupted tool round must never persist a tool_use without "
            "its tool_result — Anthropic rejects the conversation forever after",
        )
        self.assertNotIn(
            "You have two meetings.",
            self.store.assistant_texts(),
            "the cancelled turn must not persist a post-interrupt reply",
        )

    def test_interrupt_after_a_tool_round_does_not_duplicate_spoken_text(self):
        rounds = [
            _FakeStream(
                ["Let me check that. "],
                _message(
                    "tool_use",
                    [
                        _text_block("Let me check that."),
                        _tool_block("tu_1", "list_calendar_events", {"range": "today"}),
                    ],
                ),
            ),
            self._slow_stream(),
        ]
        with _env():
            with self._anthropic(rounds), patch(
                "main._run_tool",
                new_callable=AsyncMock,
                return_value=("2 events today.", None, None, []),
            ):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "mail_calendar",
                                "message": "What's on my calendar?",
                                "voice": {"enabled": False},
                            }
                        )
                        started = ws.receive_json()
                        self._read_until_delta_startswith(ws, "word")
                        ws.send_json(
                            {"type": "interrupt", "turn_id": started["turn_id"]}
                        )
                        _read_until(ws, "turn_cancelled")
                        persisted = _wait_until(
                            lambda: any(
                                "Interrupted" in text
                                for text in self.store.assistant_texts()
                            )
                        )

        self.assertTrue(persisted)
        partials = [t for t in self.store.assistant_texts() if "Interrupted" in t]
        self.assertEqual(len(partials), 1)
        self.assertTrue(partials[0].startswith("word"))
        self.assertNotIn("Let me check that.", partials[0])
        self.assertEqual(
            json.dumps(self.store.messages).count("Let me check that."),
            1,
            "text already committed with the tool round must not be stored twice",
        )

    def test_stale_interrupt_id_is_ignored(self):
        stream = _FakeStream(["All good."], _message("end_turn"))
        with _env():
            with self._anthropic([stream]):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "interrupt",
                                "turn_id": "00000000-0000-4000-8000-0000000000ff",
                            }
                        )
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Still alive?",
                                "voice": {"enabled": False},
                            }
                        )
                        frames = _read_until(ws, "response_complete")
        self.assertNotIn("turn_cancelled", [f["type"] for f in frames])
        self.assertEqual(frames[-1]["reply"], "All good.")

    def test_cancelled_turn_cannot_leak_into_the_next_turn(self):
        slow = self._slow_stream()
        fast = _FakeStream(["Second answer."], _message("end_turn"))
        session = _FakeTTSSession()
        with _env():
            with self._anthropic([slow, fast]), self._tts(session):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Long one please.",
                                "voice": {"enabled": True},
                            }
                        )
                        first_started = ws.receive_json()
                        first_turn = first_started["turn_id"]
                        ws.receive_json()
                        ws.send_json({"type": "interrupt", "turn_id": first_turn})
                        _read_until(ws, "turn_cancelled")

                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Short one now.",
                                "conversation_id": first_started["conversation_id"],
                                "voice": {"enabled": False},
                            }
                        )
                        after = _read_until(ws, "response_complete")

        self.assertTrue(after)
        second_turn = after[0]["turn_id"]
        self.assertNotEqual(second_turn, first_turn)
        self.assertEqual(
            [f["turn_id"] for f in after],
            [second_turn] * len(after),
            "a superseded turn must never emit into a newer turn",
        )
        self.assertEqual(after[-1]["reply"], "Second answer.")

    def test_echoed_conversation_id_resumes_the_interrupted_conversation(self):
        slow = self._slow_stream()
        fast = _FakeStream(["Continuing where we left off."], _message("end_turn"))
        with _env():
            with self._anthropic([slow, fast]):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Start something long.",
                                "voice": {"enabled": False},
                            }
                        )
                        started = ws.receive_json()
                        conversation_id = started["conversation_id"]
                        ws.receive_json()
                        ws.send_json({"type": "interrupt", "turn_id": started["turn_id"]})
                        _read_until(ws, "turn_cancelled")

                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Go on.",
                                "conversation_id": conversation_id,
                                "voice": {"enabled": False},
                            }
                        )
                        after = _read_until(ws, "response_complete")

        self.assertEqual(after[0]["conversation_id"], conversation_id)
        self.assertEqual(after[-1]["conversation_id"], conversation_id)
        self.assertEqual(
            list(self.store.conversations),
            [conversation_id],
            "echoing the id must not fork a new conversation",
        )
        # The resumed turn's user message lands in the same conversation as the
        # interrupted partial, so the model sees one continuous history.
        self.assertEqual(
            {m["conversation_id"] for m in self.store.messages}, {conversation_id}
        )

    def test_barge_in_with_null_conversation_id_starts_a_new_conversation(self):
        slow = self._slow_stream()
        fast = _FakeStream(["Clean slate."], _message("end_turn"))
        with _env():
            with self._anthropic([slow, fast]):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Start something long.",
                                "voice": {"enabled": False},
                            }
                        )
                        started = ws.receive_json()
                        first_conversation = started["conversation_id"]
                        self._read_until_delta_startswith(ws, "word")
                        # Implicit barge-in with no conversation_id at all.
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Forget it, new topic.",
                                "conversation_id": None,
                                "voice": {"enabled": False},
                            }
                        )
                        after = _read_until(ws, "response_complete")

        types = [f["type"] for f in after]
        self.assertIn("turn_cancelled", types)
        second_conversation = after[-1]["conversation_id"]
        # Documented in §5.1: resumption is driven by what the client sends, so
        # a null conversation_id forks a brand-new conversation.
        self.assertNotEqual(second_conversation, first_conversation)
        self.assertEqual(
            sorted(self.store.conversations),
            sorted([first_conversation, second_conversation]),
        )
        self.assertEqual(after[-1]["reply"], "Clean slate.")
        partials = [
            m
            for m in self.store.messages
            if isinstance(m["content"], str) and "Interrupted" in m["content"]
        ]
        self.assertEqual(len(partials), 1)
        self.assertEqual(
            partials[0]["conversation_id"],
            first_conversation,
            "the interrupted partial stays with the conversation it belonged to",
        )
        self.assertNotIn(
            "Forget it, new topic.",
            [
                m["content"]
                for m in self.store.messages
                if m["conversation_id"] == first_conversation
            ],
        )

    # -- connection limits --------------------------------------------------

    def test_connections_over_the_per_user_cap_are_closed_with_4029(self):
        stream = _FakeStream(["Still working."], _message("end_turn"))
        with _env():
            with self._anthropic([stream]):
                with self._client() as client:
                    with ExitStack() as stack:
                        allowed = [
                            stack.enter_context(
                                client.websocket_connect("/chat/stream")
                            )
                            for _ in range(MAX_CONNECTIONS_PER_USER)
                        ]
                        self.assertEqual(
                            OPEN_CONNECTIONS_PER_USER.get(DEV_USER),
                            MAX_CONNECTIONS_PER_USER,
                        )
                        with client.websocket_connect("/chat/stream") as extra:
                            with self.assertRaises(WebSocketDisconnect) as ctx:
                                extra.receive_json()
                        self.assertEqual(
                            ctx.exception.code, WS_CLOSE_TOO_MANY_CONNECTIONS
                        )
                        # A connection under the cap is unaffected.
                        allowed[0].send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Are you there?",
                                "voice": {"enabled": False},
                            }
                        )
                        frames = _read_until(allowed[0], "response_complete")
                        self.assertEqual(frames[-1]["reply"], "Still working.")

        self.assertEqual(
            OPEN_CONNECTIONS_PER_USER,
            {},
            "closed connections must free their slot",
        )

    def test_an_exception_in_connection_run_still_releases_the_slot(self):
        with _env():
            with patch(
                "routers.chat_stream._Connection.run",
                new=AsyncMock(side_effect=RuntimeError("forced stream failure")),
            ):
                with self._client() as client:
                    try:
                        with client.websocket_connect("/chat/stream"):
                            pass
                    except Exception:
                        pass
        self.assertEqual(OPEN_CONNECTIONS_PER_USER, {})

    def test_user_a_cap_does_not_consume_user_b_quota(self):
        user_a = "aaaaaaaa-0000-4000-8000-0000000000aa"
        user_b = "bbbbbbbb-0000-4000-8000-0000000000bb"
        OPEN_CONNECTIONS_PER_USER.clear()
        try:
            for _ in range(MAX_CONNECTIONS_PER_USER):
                self.assertTrue(_register_connection(user_a))
            self.assertFalse(_register_connection(user_a))
            self.assertTrue(
                _register_connection(user_b),
                "user B must still have a free slot when A is at the cap",
            )
            self.assertEqual(
                OPEN_CONNECTIONS_PER_USER.get(user_a), MAX_CONNECTIONS_PER_USER
            )
            self.assertEqual(OPEN_CONNECTIONS_PER_USER.get(user_b), 1)
        finally:
            _release_connection(user_b)
            for _ in range(MAX_CONNECTIONS_PER_USER):
                _release_connection(user_a)
            OPEN_CONNECTIONS_PER_USER.clear()

    def test_new_user_turn_barges_in_on_an_active_turn(self):
        slow = self._slow_stream()
        fast = _FakeStream(["Answered."], _message("end_turn"))
        with _env():
            with self._anthropic([slow, fast]):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Long story.",
                                "voice": {"enabled": False},
                            }
                        )
                        started = ws.receive_json()
                        ws.receive_json()
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Actually, never mind.",
                                "conversation_id": started["conversation_id"],
                                "voice": {"enabled": False},
                            }
                        )
                        frames = _read_until(ws, "response_complete")

        types = [f["type"] for f in frames]
        self.assertIn("turn_cancelled", types)
        self.assertEqual(frames[-1]["reply"], "Answered.")

    # -- disconnect ---------------------------------------------------------

    def test_disconnect_cleans_up_generation_and_upstream_socket(self):
        slow = self._slow_stream()
        session = _FakeTTSSession()
        with _env():
            with self._anthropic([slow]), self._tts(session):
                with self._client() as client:
                    with client.websocket_connect("/chat/stream") as ws:
                        ws.send_json(
                            {
                                "type": "user_turn",
                                "mode": "fitness",
                                "message": "Talk until I hang up.",
                                "voice": {"enabled": True},
                            }
                        )
                        ws.receive_json()
                        ws.receive_json()
                        ws.close(1000)
                        cleaned = _wait_until(lambda: slow.closed and session.closed)

        self.assertTrue(cleaned, "generation and TTS must stop on disconnect")
        self.assertTrue(slow.closed, "Claude stream must be closed on disconnect")
        self.assertTrue(slow.finalized, "text_stream generator must be finalized")
        self.assertTrue(session.closed, "ElevenLabs socket must be closed")
        self.assertFalse(
            any(
                text.startswith("word0") and "Interrupted" not in text
                for text in self.store.assistant_texts()
            ),
            "a half-written turn must never be persisted as complete",
        )


if __name__ == "__main__":
    unittest.main()
