"""WS /chat/stream — streamed chat text + streamed TTS with barge-in.

Turn machinery is reused from main.chat() (imported lazily inside handlers to
avoid a circular import), so mode validation, conversation resolution, history,
prompt layering and the Confirm Gate behave exactly like REST POST /chat. No
irreversible action ever executes over this socket: tools may only stage a
pending_action, which still requires POST /confirm.

Exactly one task reads the socket; every write goes through TurnSender, which
holds the send lock and drops frames from superseded turns.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import suppress
from typing import Any, Optional

import anthropic
from fastapi import APIRouter, Depends, HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from shared import db
from shared.auth import get_current_user_id
from shared.elevenlabs_stream import ElevenLabsStreamError, ElevenLabsStreamSession
from shared.stall_audio import (
    StallAudioError,
    UnknownStallKeyError,
    get_stall_audio,
    stall_key_for_tool,
)
from shared.streaming.phrases import SpeechChunker
from shared.streaming.protocol import (
    AUDIO_KIND_ASSISTANT,
    AUDIO_KIND_STALL,
    ERROR_FORBIDDEN_CONVERSATION,
    ERROR_INTERNAL,
    ERROR_INVALID_CONVERSATION,
    ERROR_MODEL_ERROR,
    ERROR_MODEL_UNAVAILABLE,
    ERROR_TTS_ERROR,
    ERROR_TTS_UNAVAILABLE,
    ERROR_UNSUPPORTED_MODE,
    InterruptFrame,
    ProtocolError,
    UserTurnFrame,
    error_frame,
    parse_client_frame,
    response_complete_frame,
    text_delta_frame,
    turn_started_frame,
)
from shared.streaming.turn import StreamTurn, TurnCancelled, TurnSender
from shared.tool_rounds import persist_tool_round

router = APIRouter(tags=["chat"])

# How long a cancelled generation task gets to unwind cooperatively before it
# is hard-cancelled. Cooperative unwind is preferred (it closes the Anthropic
# and ElevenLabs sockets on a normal code path).
CANCEL_GRACE_SECONDS = 5.0

# Upper bound on waiting for ElevenLabs to finish emitting a turn's audio. A
# stalled TTS provider must not hold back response_complete.
TTS_FINISH_TIMEOUT_SECONDS = 30.0

# Appended to partial assistant text persisted for an interrupted turn so the
# stored history never reads as a complete reply.
INTERRUPTED_SUFFIX = "[Interrupted by the user before this reply finished.]"

# How long a cancelled turn waits for an in-flight tool-round write to land.
# The two writes of a tool round are a pair; splitting them would persist a
# tool_use with no tool_result.

# Live /chat/stream sockets per user, so one client cannot open an unbounded
# number of Anthropic + ElevenLabs streams. In-memory and per-process by
# design: a single-process deployment with a single event loop, so no lock is
# needed and the count resets on restart.
MAX_CONNECTIONS_PER_USER = 4
OPEN_CONNECTIONS_PER_USER: dict[str, int] = {}

# Private-use close code (RFC 6455 §7.4.2) for a rejected connection over the
# per-user cap. Documented in docs/STREAMING_VOICE_V1_CONTRACT.md §1.2.
WS_CLOSE_TOO_MANY_CONNECTIONS = 4029


def _new_tts_session() -> ElevenLabsStreamSession:
    """Seam for tests — one streaming session per assistant turn."""
    return ElevenLabsStreamSession()


def _anthropic_client() -> anthropic.AsyncAnthropic:
    """Async client for streamed generations (main.py keeps the sync one)."""
    return anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


@router.websocket("/chat/stream")
async def chat_stream(
    websocket: WebSocket,
    user_id: str = Depends(get_current_user_id),
):
    """Streamed chat turns. Auth is the Bearer handshake header — never a query
    parameter — and a bad/missing token is denied with HTTP 401."""
    await websocket.accept()
    if not _register_connection(user_id):
        with suppress(RuntimeError, WebSocketDisconnect, OSError):
            await websocket.close(
                code=WS_CLOSE_TOO_MANY_CONNECTIONS,
                reason="Too many concurrent streaming connections",
            )
        return
    connection = _Connection(websocket, user_id)
    try:
        await connection.run()
    finally:
        # The slot is freed only once this socket's turn is torn down, so a
        # reconnecting client can never briefly exceed the cap.
        try:
            await connection.shutdown()
        finally:
            _release_connection(user_id)


class _Connection:
    """One websocket: single reader, at most one in-flight assistant turn."""

    def __init__(self, websocket: WebSocket, user_id: str):
        self._websocket = websocket
        self._user_id = user_id
        self._sender = TurnSender(websocket)
        self._turn: Optional[StreamTurn] = None
        self._task: Optional[asyncio.Task] = None

    # -- reader loop --------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                message = await self._websocket.receive()
            except (WebSocketDisconnect, RuntimeError):
                return
            if message.get("type") == "websocket.disconnect":
                return

            raw = message.get("text")
            if raw is None:
                raw = message.get("bytes")
            try:
                frame = parse_client_frame(raw)
            except ProtocolError as exc:
                await self._sender.send(
                    error_frame(turn_id=None, code=exc.code, message=exc.message)
                )
                if self._sender.closed:
                    return
                continue

            if isinstance(frame, InterruptFrame):
                await self._handle_interrupt(frame)
            else:
                await self._handle_user_turn(frame)
            if self._sender.closed:
                return

    async def _handle_user_turn(self, frame: UserTurnFrame) -> None:
        import main

        # A new utterance while a turn is in flight is a barge-in.
        await self._cancel_active_turn(notify=True)

        mode = frame.mode.lower().strip()
        if mode not in main.MODE_REGISTRY:
            await self._sender.send(
                error_frame(
                    turn_id=None,
                    code=ERROR_UNSUPPORTED_MODE,
                    message=(
                        f"Unsupported mode '{mode}'. "
                        f"Valid modes: {sorted(main.PUBLIC_MODE_IDS)}"
                    ),
                )
            )
            return

        turn = StreamTurn(str(uuid.uuid4()))
        self._turn = turn
        self._sender.set_active_turn(turn)
        self._task = asyncio.create_task(self._run_turn(turn, frame, mode))

    async def _handle_interrupt(self, frame: InterruptFrame) -> None:
        turn = self._turn
        if turn is None or turn.turn_id != frame.turn_id:
            # Stale or unknown turn_id: ignore. Never cancel a different turn.
            return
        await self._cancel_active_turn(notify=True)

    # -- lifecycle ----------------------------------------------------------

    async def _cancel_active_turn(self, *, notify: bool) -> None:
        turn, self._turn = self._turn, None
        task, self._task = self._task, None
        if turn is None:
            await _settle(task)
            return
        turn.cancel()
        await turn.abort_tts()
        if notify:
            await self._sender.send_cancellation(turn)
        self._sender.clear_active_turn(turn)
        await _settle(task)

    def _retire(self, turn: StreamTurn) -> None:
        if self._turn is turn:
            self._turn = None
            self._task = None
        self._sender.clear_active_turn(turn)

    async def shutdown(self) -> None:
        await self._cancel_active_turn(notify=False)
        self._sender.mark_closed()
        if self._websocket.client_state is WebSocketState.CONNECTED:
            with suppress(RuntimeError, WebSocketDisconnect, OSError):
                await self._websocket.close()

    # -- one assistant turn -------------------------------------------------

    async def _run_turn(
        self, turn: StreamTurn, frame: UserTurnFrame, mode: str
    ) -> None:
        voice = _TurnVoice(turn=turn, sender=self._sender, requested=frame.voice.enabled)
        try:
            await self._generate(turn, frame, mode, voice)
        except TurnCancelled:
            pass
        except asyncio.CancelledError:
            raise
        except HTTPException as exc:
            await self._sender.send(
                error_frame(
                    turn_id=turn.turn_id,
                    code=_http_error_code(exc.status_code),
                    message=str(exc.detail),
                ),
                turn=turn,
            )
        except db.DatabaseUnavailableError:
            await self._sender.send(
                error_frame(
                    turn_id=turn.turn_id,
                    code=ERROR_INTERNAL,
                    message="Database temporarily unavailable.",
                ),
                turn=turn,
            )
        except Exception:
            # Never leak model/user content into an error message.
            await self._sender.send(
                error_frame(
                    turn_id=turn.turn_id,
                    code=ERROR_INTERNAL,
                    message="The turn failed. Try again.",
                ),
                turn=turn,
            )
        finally:
            await voice.aclose()
            self._retire(turn)

    async def _generate(
        self,
        turn: StreamTurn,
        frame: UserTurnFrame,
        request_mode: str,
        voice: "_TurnVoice",
    ) -> None:
        import main

        user_id = self._user_id
        transcript = frame.message

        try:
            conversation_id, mode = await main._ensure_conversation(
                frame.conversation_id, user_id=user_id, mode=request_mode
            )
        except (HTTPException, db.DatabaseUnavailableError) as exc:
            # turn_started has not been sent, so the client has no state for
            # this turn_id yet. Report it connection-level (turn_id: null) —
            # an error frame must never introduce an unknown turn_id. A
            # Postgres blip here is routine, not exotic, so it takes the same
            # path as a rejected conversation id.
            if isinstance(exc, HTTPException):
                code = _http_error_code(exc.status_code)
                message = str(exc.detail)
            else:
                code = ERROR_INTERNAL
                message = "Database temporarily unavailable."
            await self._sender.send(
                error_frame(turn_id=None, code=code, message=message)
            )
            return
        turn.conversation_id = conversation_id
        turn.raise_if_cancelled()
        await self._sender.send(
            turn_started_frame(turn_id=turn.turn_id, conversation_id=conversation_id),
            turn=turn,
        )
        turn.raise_if_cancelled()

        # History reopen before screen navigate — more specific (mirrors chat()).
        open_intent = main.parse_open_conversation_command(transcript)
        if open_intent is not None:
            await db.append_message(conversation_id, "user", transcript)
            await main._maybe_set_title(conversation_id, mode=mode, user_text=transcript)
            bounds = main.day_bounds_utc(open_intent.day)
            candidates = await db.find_conversations_for_open(
                user_id,
                mode=open_intent.mode,
                started_after=bounds[0] if bounds else None,
                started_before=bounds[1] if bounds else None,
                limit=10,
            )
            if (
                open_intent.mode is None
                and open_intent.day == "any"
                and open_intent.most_recent
            ):
                candidates = candidates[:1]
            resolution = main.resolve_open_conversation(candidates, intent=open_intent)
            if resolution.conversation_id:
                reply = main.open_conversation_acknowledgement(resolution.mode)
                actions = [main.open_conversation_action(resolution.conversation_id)]
            else:
                reply = resolution.clarify_reply or "I couldn't find that conversation."
                actions = main.empty_client_actions()
            await db.append_message(conversation_id, "assistant", reply)
            await self._deliver_reply(
                turn,
                voice,
                reply=reply,
                conversation_id=conversation_id,
                client_actions=actions,
            )
            return

        navigate = main.parse_navigate_command(transcript)
        if navigate is not None:
            await db.append_message(conversation_id, "user", transcript)
            await main._maybe_set_title(conversation_id, mode=mode, user_text=transcript)
            if navigate.target is not None:
                reply = main.navigate_acknowledgement(navigate.target)
                actions = [main.navigate_action(navigate.target)]
            else:
                reply = main.blocked_navigate_reply(navigate.blocked_alias or "")
                actions = main.empty_client_actions()
            await db.append_message(conversation_id, "assistant", reply)
            await self._deliver_reply(
                turn,
                voice,
                reply=reply,
                conversation_id=conversation_id,
                client_actions=actions,
            )
            return

        if not os.environ.get("ANTHROPIC_API_KEY"):
            await self._sender.send(
                error_frame(
                    turn_id=turn.turn_id,
                    code=ERROR_MODEL_UNAVAILABLE,
                    message="ANTHROPIC_API_KEY not configured",
                ),
                turn=turn,
            )
            return

        convo = await db.get_conversation(conversation_id)
        summary_text = (convo or {}).get("summary_text")
        summary_through_seq = (convo or {}).get("summary_through_seq")
        messages_with_seq = await db.load_messages_with_seq(conversation_id)

        async def _persist_summary(text: str, through: int) -> None:
            await db.update_conversation_summary(
                conversation_id, summary_text=text, summary_through_seq=through
            )

        summary_text, summary_through_seq, _ = await main.maybe_roll_summary(
            conversation_id=conversation_id,
            messages_with_seq=messages_with_seq,
            summary_text=summary_text,
            summary_through_seq=summary_through_seq,
            persist=_persist_summary,
        )

        await db.append_message(conversation_id, "user", transcript)
        await main._maybe_set_title(conversation_id, mode=mode, user_text=transcript)

        floor = -1 if summary_through_seq is None else int(summary_through_seq)
        prior = [
            {"role": m["role"], "content": m["content"]}
            for m in messages_with_seq
            if int(m["seq"]) > floor
        ]

        profile = await main.get_profile(user_id)
        profile_block = main.compact_profile_for_context(profile)
        checkin_block = ""
        if mode in ("fitness", "diet"):
            try:
                today_checkin = await main.get_today_checkin(user_id)
                checkin_block = main.compact_checkin_for_context(today_checkin)
            except Exception:
                checkin_block = ""
        try:
            user_customization_block = await main.load_active_customization_block(
                user_id, mode
            )
        except Exception:
            # Prompt overrides must never break chat.
            user_customization_block = ""
        system_prompt = main._build_system_prompt(
            mode,
            profile_block=profile_block,
            checkin_block=checkin_block,
            user_customization_block=user_customization_block,
        )

        built = main.build_model_messages(
            system_prompt=system_prompt,
            profile_block=profile_block,
            summary_text=summary_text,
            summary_through_seq=summary_through_seq,
            recent_messages=prior,
            current_user_message={"role": "user", "content": transcript},
        )
        context_meta = {
            "raw_messages_included": built.raw_messages_included,
            "summary_used": built.summary_used,
            "summary_through_seq": built.summary_through_seq,
            "approx_context_utilization": built.approx_context_utilization,
            "estimated_message_tokens": built.estimated_message_tokens,
            "estimated_system_tokens": built.estimated_system_tokens,
        }

        turn.raise_if_cancelled()

        if mode == "brainstorm" and main.wants_research(transcript):
            provider = main.get_research_provider()
            if provider is None:
                research_turn = main.unavailable_turn(
                    transcript,
                    reason=(
                        "I can't look that up right now — web research isn't "
                        "available. We can keep brainstorming without a live search."
                    ),
                )
            else:
                research_turn = await provider.research(
                    main.clamp_research_query(transcript),
                    claim=main.extract_claim(transcript),
                )
            turn.raise_if_cancelled()
            research = main.sanitize_research(
                research_turn.research,
                provider_called=research_turn.research.status != "unavailable",
            )
            await db.append_message(conversation_id, "assistant", research_turn.reply)
            await self._deliver_reply(
                turn,
                voice,
                reply=research_turn.reply,
                conversation_id=conversation_id,
                research=research.model_dump(mode="json") if research else None,
            )
            return

        await self._stream_model_turn(
            turn,
            voice,
            mode=mode,
            conversation_id=conversation_id,
            history_for_model=built.messages,
            system_prompt=system_prompt,
            context_meta=context_meta,
        )

    async def _stream_model_turn(
        self,
        turn: StreamTurn,
        voice: "_TurnVoice",
        *,
        mode: str,
        conversation_id: str,
        history_for_model: list[dict],
        system_prompt: str,
        context_meta: dict[str, Any],
    ) -> None:
        """Streamed mirror of main._run_model_turn (same tool loop semantics)."""
        import main

        create_kwargs: dict = dict(
            model=main.MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=list(history_for_model),
        )
        tools = main.MODE_TOOLS.get(mode)
        if tools:
            create_kwargs["tools"] = tools

        spoken_parts: list[str] = []
        # Rounds already committed to history inside their assistant blocks —
        # re-persisting them on interrupt would duplicate the spoken text.
        persisted_through = 0
        pending_action = None
        visual_panel = None
        client_actions: list = []
        input_tokens: Optional[int] = None
        output_tokens: Optional[int] = None
        working = list(history_for_model)
        final_text = ""

        await voice.start()
        client = _anthropic_client()
        try:
            while True:
                turn.raise_if_cancelled()
                round_text, message = await self._stream_one_round(
                    turn, voice, client, create_kwargs, spoken_parts
                )
                usage = getattr(message, "usage", None)
                input_tokens = getattr(usage, "input_tokens", input_tokens)
                out = getattr(usage, "output_tokens", None)
                if out is not None:
                    output_tokens = (output_tokens or 0) + out

                if message.stop_reason != "tool_use":
                    final_text = round_text.strip()
                    break

                turn.raise_if_cancelled()
                tool_blocks = [b for b in message.content if b.type == "tool_use"]
                if tool_blocks and not round_text.strip():
                    # Only fill silence: if the model already said what it was
                    # doing, a stall phrase would just repeat it.
                    await voice.speak_stall(tool_blocks[0].name)
                else:
                    await voice.flush()

                tool_results = []
                for block in tool_blocks:
                    result_text, created, panel, actions = await main._run_tool(
                        block.name,
                        block.input,
                        user_id=self._user_id,
                        conversation_id=conversation_id,
                        mode=mode,
                    )
                    if created is not None:
                        pending_action = created
                    if panel is not None:
                        visual_panel = panel
                    if actions:
                        client_actions.extend(actions)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        }
                    )

                # The assistant tool_use blocks and their tool_result blocks are
                # a pair: history containing one without the other is rejected
                # by Anthropic for the whole conversation from then on. The
                # write is shielded so a hard cancel cannot split it; a failed
                # write is repaired on read by shared.context_budget.
                assistant_blocks = [block.model_dump() for block in message.content]
                await _persist_tool_round(
                    conversation_id,
                    assistant_blocks=assistant_blocks,
                    tool_results=tool_results,
                )
                # This round's text is now in history inside assistant_blocks.
                persisted_through = len(spoken_parts)
                working.append({"role": "assistant", "content": assistant_blocks})
                working.append({"role": "user", "content": tool_results})
                create_kwargs["messages"] = working
        except TurnCancelled:
            await self._persist_interrupted(
                conversation_id, spoken_parts[persisted_through:]
            )
            raise
        finally:
            with suppress(Exception):
                await client.close()

        if not final_text:
            await self._sender.send(
                error_frame(
                    turn_id=turn.turn_id,
                    code=ERROR_MODEL_ERROR,
                    message="Model returned no text",
                ),
                turn=turn,
            )
            return

        await db.append_message(conversation_id, "assistant", final_text)
        try:
            await db.insert_turn_metrics(
                conversation_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw_messages_included=int(context_meta.get("raw_messages_included") or 0),
                summary_used=bool(context_meta.get("summary_used")),
                summary_through_seq=context_meta.get("summary_through_seq"),
                approx_context_utilization=context_meta.get(
                    "approx_context_utilization"
                ),
                supplemental={
                    "estimated_message_tokens": context_meta.get(
                        "estimated_message_tokens"
                    ),
                    "estimated_system_tokens": context_meta.get(
                        "estimated_system_tokens"
                    ),
                },
            )
        except Exception:
            # Metrics must never break chat.
            pass

        await self._deliver_reply(
            turn,
            voice,
            reply="\n\n".join(spoken_parts),
            conversation_id=conversation_id,
            pending_action=pending_action.model_dump() if pending_action else None,
            visual_panel=(
                visual_panel.model_dump(mode="json") if visual_panel else None
            ),
            client_actions=_dedupe_actions(client_actions),
            already_spoken=True,
        )

    async def _stream_one_round(
        self,
        turn: StreamTurn,
        voice: "_TurnVoice",
        client: anthropic.AsyncAnthropic,
        create_kwargs: dict,
        spoken_parts: list[str],
    ) -> tuple[str, Any]:
        """Stream one model response; returns (text, final Message)."""
        round_text = ""
        async with client.messages.stream(**create_kwargs) as stream:
            async for delta in stream.text_stream:
                if turn.cancelled:
                    # Explicit close on a normal code path aborts the upstream
                    # HTTP request without relying on task cancellation.
                    await stream.close()
                    if round_text.strip():
                        spoken_parts.append(round_text.strip())
                    raise TurnCancelled(turn.turn_id)
                if not delta:
                    continue
                round_text += delta
                await self._sender.send(
                    text_delta_frame(turn_id=turn.turn_id, delta=delta), turn=turn
                )
                await voice.speak(delta)
            message = await stream.get_final_message()
        if round_text.strip():
            spoken_parts.append(round_text.strip())
        return round_text, message

    async def _persist_interrupted(
        self, conversation_id: str, spoken_parts: list[str]
    ) -> None:
        """Store what was actually said before the interrupt, marked partial."""
        partial = "\n\n".join(part for part in spoken_parts if part.strip()).strip()
        if not partial:
            return
        with suppress(Exception):
            await db.append_message(
                conversation_id, "assistant", f"{partial}\n\n{INTERRUPTED_SUFFIX}"
            )

    async def _deliver_reply(
        self,
        turn: StreamTurn,
        voice: "_TurnVoice",
        *,
        reply: str,
        conversation_id: str,
        pending_action: dict | None = None,
        visual_panel: dict | None = None,
        research: dict | None = None,
        client_actions: list | None = None,
        already_spoken: bool = False,
    ) -> None:
        """Emit any remaining text, finish the audio stream, then complete."""
        turn.raise_if_cancelled()
        if not already_spoken:
            await voice.start()
            await self._sender.send(
                text_delta_frame(turn_id=turn.turn_id, delta=reply), turn=turn
            )
            await voice.speak(reply)
        await voice.finish()
        turn.raise_if_cancelled()
        await self._sender.send(
            response_complete_frame(
                turn_id=turn.turn_id,
                conversation_id=conversation_id,
                reply=reply,
                pending_action=pending_action,
                visual_panel=visual_panel,
                research=research,
                client_actions=[_as_wire(action) for action in (client_actions or [])],
            ),
            turn=turn,
        )


class _TurnVoice:
    """One ElevenLabs streaming session plus its audio pump, for one turn.

    Every failure here is non-fatal: the socket gets a tts error frame and the
    text half of the turn continues to completion.
    """

    def __init__(self, *, turn: StreamTurn, sender: TurnSender, requested: bool):
        self._turn = turn
        self._sender = sender
        self._requested = requested
        self._session: Optional[ElevenLabsStreamSession] = None
        self._consumer: Optional[asyncio.Task] = None
        self._chunker = SpeechChunker()
        self._stalls_emitted: set[str] = set()
        self._started = False

    @property
    def streaming(self) -> bool:
        return self._session is not None

    async def start(self) -> None:
        if not self._requested or self._started or self._turn.cancelled:
            return
        self._started = True
        session = _new_tts_session()
        # Registered before open() so every teardown path — cooperative abort,
        # this turn's aclose(), or a hard cancel delivered inside open()'s init
        # send — can still reach a socket that already connected upstream.
        self._session = session
        self._turn.attach_tts(session)
        try:
            await session.open()
        except HTTPException as exc:
            await self._discard(session)
            await self._report(ERROR_TTS_UNAVAILABLE, str(exc.detail))
            return
        except ElevenLabsStreamError:
            await self._discard(session)
            await self._report(ERROR_TTS_UNAVAILABLE, "Voice streaming is unavailable.")
            return
        except asyncio.CancelledError:
            # A hard cancel here would otherwise leak a connected upstream
            # websocket that nothing holds a reference to any more.
            await self._discard(session)
            raise
        except BaseException:
            await self._discard(session)
            raise
        self._consumer = asyncio.create_task(self._consume())

    async def _discard(self, session: ElevenLabsStreamSession) -> None:
        """Forget a session that never became usable, closing it upstream."""
        if self._session is session:
            self._session = None
        self._turn.attach_tts(None)
        with suppress(BaseException):
            await session.close()

    async def speak(self, delta: str) -> None:
        session = self._session
        if session is None or self._turn.cancelled:
            return
        for chunk in self._chunker.push(delta):
            try:
                await session.send_text(chunk)
            except ElevenLabsStreamError:
                await self._fail()
                return

    async def flush(self) -> None:
        session = self._session
        if session is None or self._turn.cancelled:
            return
        try:
            await session.flush(self._chunker.drain())
        except ElevenLabsStreamError:
            await self._fail()

    async def speak_stall(self, tool_name: str) -> None:
        """Emit the cached phrase for the tool that is actually being invoked."""
        if not self._requested or self._turn.cancelled:
            return
        key = stall_key_for_tool(tool_name)
        if key in self._stalls_emitted:
            return
        self._stalls_emitted.add(key)
        try:
            audio = await get_stall_audio(key)
        except (UnknownStallKeyError, StallAudioError, HTTPException, OSError):
            return
        if self._turn.cancelled:
            return
        await self._sender.send_audio(self._turn, kind=AUDIO_KIND_STALL, audio=audio)

    async def finish(self) -> None:
        """Flush the tail, then wait for the provider's final audio frame."""
        session, consumer = self._session, self._consumer
        if session is None:
            return
        if not self._turn.cancelled:
            try:
                await session.flush_and_finish(self._chunker.drain())
            except ElevenLabsStreamError:
                await self._fail()
                return
        if consumer is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(consumer), timeout=TTS_FINISH_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await self._report(ERROR_TTS_ERROR, "Voice stream timed out.")
            await session.abort()
            consumer.cancel()
            await _settle(consumer)

    async def aclose(self) -> None:
        session, self._session = self._session, None
        consumer, self._consumer = self._consumer, None
        if session is not None:
            with suppress(Exception):
                await session.close()
        if consumer is not None:
            consumer.cancel()
            await _settle(consumer)

    async def _consume(self) -> None:
        session = self._session
        if session is None:
            return
        try:
            async for audio in session.audio_chunks():
                if self._turn.cancelled:
                    return
                sent = await self._sender.send_audio(
                    self._turn, kind=AUDIO_KIND_ASSISTANT, audio=audio
                )
                if not sent:
                    return
        except ElevenLabsStreamError:
            if not self._turn.cancelled:
                await self._report(ERROR_TTS_ERROR, "Voice stream failed.")
        except asyncio.CancelledError:
            raise

    async def _fail(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            with suppress(Exception):
                await session.abort()
        await self._report(ERROR_TTS_ERROR, "Voice stream failed.")

    async def _report(self, code: str, message: str) -> None:
        await self._sender.send(
            error_frame(turn_id=self._turn.turn_id, code=code, message=message),
            turn=self._turn,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _http_error_code(status_code: int) -> str:
    """Map an HTTPException from the shared turn machinery to a wire code.

    503 is deliberately not `tts_unavailable`: TTS failures are handled inside
    _TurnVoice and never reach here, so a 503 arriving at this point is a dead
    turn. Labelling it `tts_unavailable` would tell the client the turn is still
    coming (a response_complete always follows that code) and hang it forever.
    """
    if status_code == 400:
        return ERROR_INVALID_CONVERSATION
    if status_code == 403:
        return ERROR_FORBIDDEN_CONVERSATION
    return ERROR_INTERNAL


def _register_connection(user_id: str) -> bool:
    """Claim one of this user's connection slots. False means over the cap."""
    live = OPEN_CONNECTIONS_PER_USER.get(user_id, 0)
    if live >= MAX_CONNECTIONS_PER_USER:
        return False
    OPEN_CONNECTIONS_PER_USER[user_id] = live + 1
    return True


def _release_connection(user_id: str) -> None:
    live = OPEN_CONNECTIONS_PER_USER.get(user_id, 0) - 1
    if live > 0:
        OPEN_CONNECTIONS_PER_USER[user_id] = live
    else:
        OPEN_CONNECTIONS_PER_USER.pop(user_id, None)


async def _persist_tool_round(
    conversation_id: str, *, assistant_blocks: list, tool_results: list
) -> None:
    """Thin alias so the streaming turn and `main` share one implementation."""
    await persist_tool_round(
        conversation_id,
        assistant_blocks=assistant_blocks,
        tool_results=tool_results,
    )


def _as_wire(action: Any) -> dict:
    dump = getattr(action, "model_dump", None)
    return dump(mode="json") if callable(dump) else dict(action)


def _dedupe_actions(actions: list) -> list:
    """Drop repeated refresh_profile (mirrors main._run_model_turn)."""
    deduped: list = []
    seen_refresh = False
    for action in actions:
        if getattr(action, "type", None) == "refresh_profile":
            if seen_refresh:
                continue
            seen_refresh = True
        deduped.append(action)
    return deduped


async def _settle(task: Optional[asyncio.Task]) -> None:
    """Await a finished/cancelled task so nothing outlives the handler."""
    if task is None:
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(task), timeout=CANCEL_GRACE_SECONDS
        )
    except asyncio.TimeoutError:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    except asyncio.CancelledError:
        # The handler itself is being torn down: still cancel and reap the
        # child so no generation task outlives the connection.
        if not task.done():
            task.cancel()
            with suppress(BaseException):
                await task
        raise
    except Exception:
        # The task already reported its own failure over the socket.
        pass
