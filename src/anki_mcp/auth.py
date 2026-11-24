from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """Reject oversized MCP request bodies before JSON parsing or tool dispatch."""

    def __init__(self, app: ASGIApp, max_bytes: int, mcp_path: str = "/mcp") -> None:
        self.app = app
        self._max_bytes = max_bytes
        self._path = mcp_path.rstrip("/")

    @property
    def state(self) -> Any:
        """Preserve Starlette state access through the middleware wrapper."""
        return self.app.state  # type: ignore[attr-defined]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        limited = (
            scope["type"] == "http"
            and scope.get("method") in {"POST", "PUT", "PATCH"}
            and (path == self._path or path.startswith(self._path + "/"))
        )
        if not limited:
            await self.app(scope, receive, send)
            return

        buffered: list[Message] = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self._max_bytes:
                    response = JSONResponse({"error": "request_too_large"}, status_code=413)
                    await response(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            else:
                break

        async def replay() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.app(scope, replay, send)
