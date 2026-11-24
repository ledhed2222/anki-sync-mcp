from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
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


_OWNER_PASSWORD_HASH: list[str] = []
OWNER_PASSWORD = "correct horse battery"


def owner_password_hash() -> str:
    if not _OWNER_PASSWORD_HASH:
        from anki_mcp.oauth import hash_password

        _OWNER_PASSWORD_HASH.append(hash_password(OWNER_PASSWORD))
    return _OWNER_PASSWORD_HASH[0]


def owner_collection_path(data_dir: Path) -> Path:
    """Where the `owner` user's collection lives; seed it here before starting the app."""
    path = data_dir / "users" / "owner" / "collection.anki2"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def sign_in_users(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, tokens: dict[str, str], **user_fields: Any
) -> None:
    """Point the app at `data_dir` with these users, each already signed in with its token."""
    data_dir.mkdir(parents=True, exist_ok=True)
    entry = {"password_hash": owner_password_hash(), **user_fields}
    users = {user_id: dict(entry) for user_id in tokens}
    (data_dir / "users.json").write_text(json.dumps({"users": users}))
    access = {
        hashlib.sha256(token.encode()).hexdigest(): {
            "client_id": "pytest",
            "scopes": ["read", "write", "admin", "destructive"],
            "expires_at": int(time.time()) + 86_400,
            "refresh_key": "",
            "subject": user_id,
        }
        for user_id, token in tokens.items()
    }
    (data_dir / "oauth.json").write_text(
        json.dumps({"clients": {}, "access": access, "refresh": {}})
    )
    monkeypatch.setenv("ANKI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://localhost")


def use_owner(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, token: str, **user_fields: Any
) -> None:
    """Point the app at `data_dir` with one `owner` user who is already signed in as `token`."""
    sign_in_users(monkeypatch, data_dir, {"owner": token}, **user_fields)


def owner_service(client: Any) -> Any:
    """The `owner` user's open collection service, from inside a running TestClient."""
    app = client.app
    while not hasattr(app, "state"):
        app = app.app
    registry = app.state.collection_registry

    async def lease() -> Any:
        async with registry.lease("owner") as service:
            return service

    return client.portal.call(lease)
