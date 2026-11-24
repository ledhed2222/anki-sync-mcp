from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Any

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
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


class BearerAuthMiddleware:
    """Authenticate every request under the configured MCP endpoint."""

    def __init__(
        self,
        app: ASGIApp,
        token: str,
        mcp_path: str = "/mcp",
        resolve_user: Callable[[str], str | None] | None = None,
    ) -> None:
        self.app = app
        self._expected = token.encode("utf-8")
        self._path = mcp_path.rstrip("/")
        self._resolve_user = resolve_user

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == self._path or path.startswith(self._path + "/")):
            headers = dict(scope.get("headers", []))
            authorization = headers.get(b"authorization", b"")
            scheme, separator, supplied = authorization.partition(b" ")
            has_bearer_scheme = separator == b" " and scheme.lower() == b"bearer"
            if self._resolve_user is not None:
                user_id = self._resolve_user(supplied.decode("utf-8", "replace"))
                valid = has_bearer_scheme and user_id is not None
                if valid:
                    scope["user"] = AuthenticatedUser(
                        AccessToken(token="", client_id="bearer", scopes=[], subject=user_id)
                    )
            else:
                # Always execute compare_digest, including malformed/missing credentials.
                token_matches = hmac.compare_digest(supplied, self._expected)
                valid = has_bearer_scheme and token_matches
            if not valid:
                response = JSONResponse(
                    {"error": "authentication_failed"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
