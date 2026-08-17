"""Split streamed model text into TTS-safe chunks.

ElevenLabs stream-input requires each incremental `text` to end in a space (it
uses the trailing space as a word boundary), so deltas are buffered until a
whole word is available rather than forwarded verbatim.
"""

from __future__ import annotations

# Below this the chunk is not worth a websocket frame; ElevenLabs buffers to its
# chunk_length_schedule anyway, so batching words costs no added latency.
MIN_CHUNK_CHARS = 24


class SpeechChunker:
    """Accumulate text deltas and hand back space-terminated chunks."""

    def __init__(self, min_chunk_chars: int = MIN_CHUNK_CHARS):
        self._buffer = ""
        self._min_chunk_chars = max(1, min_chunk_chars)

    def push(self, delta: str) -> list[str]:
        """Add a delta; return zero or more chunks each ending in a space."""
        if not delta:
            return []
        self._buffer += delta
        chunks: list[str] = []
        while len(self._buffer) >= self._min_chunk_chars:
            cut = self._buffer.rfind(" ")
            if cut < 0:
                break
            chunk = self._buffer[: cut + 1]
            self._buffer = self._buffer[cut + 1 :]
            if chunk.strip():
                chunks.append(chunk)
            else:
                # Whitespace-only run: keep it attached to the next chunk.
                self._buffer = chunk + self._buffer
                break
        return chunks

    def drain(self) -> str:
        """Return the trailing partial word (also space-terminated) and reset."""
        remainder, self._buffer = self._buffer, ""
        if not remainder.strip():
            return ""
        if not remainder.endswith(" "):
            remainder += " "
        return remainder

    @property
    def pending(self) -> str:
        return self._buffer
