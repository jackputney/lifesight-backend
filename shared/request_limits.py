"""Path-scoped ASGI body ceiling for POST /healthkit/sync.

This is a pure ASGI middleware — not BaseHTTPMiddleware and not
`@app.middleware("http")`. Starlette's HTTP middleware buffers the full
body before the route runs, so a Content-Length-only check cannot stop a
chunked or header-less upload from materializing tens of MB.

Scoped to POST /healthkit/sync only: POST /food/photo sends base64 images
that would break under a tight global limit, and ordinary JSON routes
must stay unaffected.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

from shared.health.service import HEALTHKIT_SYNC_MAX_BODY_BYTES

ASGIApp = Callable[
    [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]],
    Awaitable[None],
]

_HEALTHKIT_SYNC_PATH = "/healthkit/sync"


def _header_map(scope: dict[str, Any]) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


async def _send_json(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    status: int,
    detail: str,
) -> None:
    payload = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def _drain(receive: Callable[[], Awaitable[dict[str, Any]]]) -> None:
    """Consume remaining request chunks without retaining them."""
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            return
        more = bool(message.get("more_body"))


class HealthKitSyncBodyLimitMiddleware:
    """Reject oversized POST /healthkit/sync bodies before the route runs."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") != "POST" or scope.get("path") != _HEALTHKIT_SYNC_PATH:
            await self.app(scope, receive, send)
            return

        headers = _header_map(scope)
        declared = headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                await _send_json(send, 400, "Invalid Content-Length header")
                return
            if length < 0:
                await _send_json(send, 400, "Invalid Content-Length header")
                return
            if length > HEALTHKIT_SYNC_MAX_BODY_BYTES:
                await _send_json(
                    send,
                    413,
                    (
                        "Request body too large "
                        f"(limit {HEALTHKIT_SYNC_MAX_BODY_BYTES} bytes)."
                    ),
                )
                return

        body, overflowed = await _read_capped(receive, HEALTHKIT_SYNC_MAX_BODY_BYTES)
        if overflowed:
            await _send_json(
                send,
                413,
                (
                    "Request body too large "
                    f"(limit {HEALTHKIT_SYNC_MAX_BODY_BYTES} bytes)."
                ),
            )
            return
        if body is None:
            return

        replayed = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


async def _read_capped(
    receive: Callable[[], Awaitable[dict[str, Any]]],
    ceiling: int,
) -> tuple[Optional[bytes], bool]:
    """Read the request body, retaining at most `ceiling` bytes.

    Returns (body, overflowed). overflowed=True means the ceiling was
    exceeded; remaining chunks were drained and not stored. body is None
    when the client disconnected before a complete request arrived.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None, False
        if message["type"] != "http.request":
            continue
        piece = message.get("body", b"") or b""
        more = bool(message.get("more_body"))
        if total + len(piece) > ceiling:
            if more:
                await _drain(receive)
            return None, True
        if piece:
            chunks.append(piece)
            total += len(piece)
        if not more:
            return b"".join(chunks), False
