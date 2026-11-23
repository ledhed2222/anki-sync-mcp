from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from anki_mcp import app as app_module


@pytest.fixture(autouse=True)
def _read_sse_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let `.json()` read MCP responses sent as SSE, used whenever full-sync tools exist."""
    original = httpx.Response.json

    def json_or_sse(response: httpx.Response, **kwargs: Any) -> Any:
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            data = [line[5:] for line in response.text.splitlines() if line.startswith("data:")]
            return json.loads(data[-1])
        return original(response, **kwargs)

    monkeypatch.setattr(httpx.Response, "json", json_or_sse)


@pytest.fixture
def person_approves(monkeypatch: pytest.MonkeyPatch) -> None:
    async def approve(*_: Any) -> bool:
        return True

    monkeypatch.setattr(app_module, "_ask_person", approve)
