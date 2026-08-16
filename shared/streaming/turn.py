"""Per-turn cancellation tokens and the single outbound-frame choke-point.

Starlette's WebSocket has no internal send lock (concurrent sends corrupt its
state machine) and a barge-in must never let a superseded turn leak audio or
text into the next one — so every frame goes through TurnSender.send, which
holds an asyncio.Lock and drops frames that do not belong to the active turn.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional, Protocol, runtime_checkable

from starlette.websockets import WebSocket, WebSocketDisconnect

from shared.streaming.protocol import (
    FRAME_AUDIO_CHUNK,
    FRAME_TURN_CANCELLED,
    audio_chunk_frame,
    turn_cancelled_frame,
)


class TurnCancelled(Exception):
    """Raised inside a generation task once its turn has been interrupted."""


@runtime_checkable
class AbortableStream(Protocol):
    """Anything holding an upstream socket that must die with the turn."""

    async def abort(self) -> None: ...


class StreamTurn:
    """One assistant turn: identity, cancellation token, audio sequencing."""

    def __init__(self, turn_id: str, *, conversation_id: str | None = None):
        self.turn_id = turn_id
        self.conversation_id = conversation_id
        self._cancelled = asyncio.Event()
        self._cancel_notified = False
        self._audio_sequence = -1
        self._tts: Optional[AbortableStream] = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def cancel_notified(self) -> bool:
        return self._cancel_notified

    def mark_cancel_notified(self) -> bool:
        """True the first time only — guarantees exactly one turn_cancelled."""
        if self._cancel_notified:
            return False
        self._cancel_notified = True
        return True

    def cancel(self) -> None:
        self._cancelled.set()

    async def wait_cancelled(self) -> None:
        await self._cancelled.wait()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TurnCancelled(self.turn_id)

    def next_audio_sequence(self) -> int:
        self._audio_sequence += 1
        return self._audio_sequence

    def attach_tts(self, session: AbortableStream | None) -> None:
        self._tts = session

    async def abort_tts(self) -> None:
        """Close this turn's upstream TTS socket. Safe to call repeatedly."""
        session, self._tts = self._tts, None
        if session is None:
            return
        try:
            await session.abort()
        except Exception:
            # A failing abort must never block cancellation of the turn.
            pass


def frame_allowed(
    *,
    frame_type: str,
    turn: StreamTurn | None,
    active_turn_id: str | None,
) -> bool:
    """Stale-turn guard. Pure so the barge-in rules are unit-testable.

    Connection-level frames (turn=None) always pass. Turn frames pass only for
    the currently active turn; a cancelled turn may still emit its single
    turn_cancelled frame and nothing else.
    """
    if turn is None:
        return True
    if active_turn_id != turn.turn_id:
        return False
    if frame_type == FRAME_TURN_CANCELLED:
        return True
    return not turn.cancelled


class TurnSender:
    """Serializes every server → client frame and enforces frame_allowed."""

    def __init__(self, websocket: WebSocket):
        self._websocket = websocket
        self._lock = asyncio.Lock()
        self._active_turn_id: str | None = None
        self._closed = False

    @property
    def active_turn_id(self) -> str | None:
        return self._active_turn_id

    @property
    def closed(self) -> bool:
        return self._closed

    def set_active_turn(self, turn: StreamTurn) -> None:
        self._active_turn_id = turn.turn_id

    def clear_active_turn(self, turn: StreamTurn) -> None:
        if self._active_turn_id == turn.turn_id:
            self._active_turn_id = None

    def mark_closed(self) -> None:
        self._closed = True

    async def send(self, frame: dict[str, Any], *, turn: StreamTurn | None = None) -> bool:
        """Send one frame. False means it was dropped (stale turn or dead socket)."""
        async with self._lock:
            if not self._permits(str(frame.get("type")), turn):
                return False
            return await self._emit(frame)

    async def send_audio(self, turn: StreamTurn, *, kind: str, audio: bytes) -> bool:
        """Send one audio chunk, numbering it inside the lock.

        Two tasks produce audio (streamed speech and cached stall clips), so the
        sequence must be allocated at send time or the client can observe a
        lower number after a higher one.
        """
        async with self._lock:
            if not self._permits(FRAME_AUDIO_CHUNK, turn):
                return False
            frame = audio_chunk_frame(
                turn_id=turn.turn_id,
                sequence=turn.next_audio_sequence(),
                kind=kind,
                audio=audio,
            )
            return await self._emit(frame)

    async def send_cancellation(self, turn: StreamTurn) -> bool:
        """Emit exactly one turn_cancelled for `turn`."""
        if not turn.mark_cancel_notified():
            return False
        return await self.send(turn_cancelled_frame(turn_id=turn.turn_id), turn=turn)

    # -- internals (lock held) ----------------------------------------------

    def _permits(self, frame_type: str, turn: StreamTurn | None) -> bool:
        if self._closed:
            return False
        return frame_allowed(
            frame_type=frame_type,
            turn=turn,
            active_turn_id=self._active_turn_id,
        )

    async def _emit(self, frame: dict[str, Any]) -> bool:
        try:
            await self._websocket.send_json(frame)
        except (WebSocketDisconnect, RuntimeError, OSError):
            # Sends raise on disconnect too (1006 on OSError), not just
            # receives. Treat the socket as gone and stop writing.
            self._closed = True
            return False
        return True
