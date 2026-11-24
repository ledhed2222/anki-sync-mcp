from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import OWNER_PASSWORD as PASSWORD
from conftest import sign_in_users
from starlette.testclient import TestClient

from anki_mcp import oauth, users
from anki_mcp.app import _link_media_folder, create_app
from anki_mcp.collection import ResourceLimitError
from anki_mcp.config import Settings
from anki_mcp.users import CollectionRegistry

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


class _FakeService:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.closed = False
        self.syncing_media = False

    async def media_sync_active(self) -> bool:
        return self.syncing_media

    async def __aexit__(self, *_: object) -> None:
        self.closed = True


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _registry(max_open: int = 2, idle: float = 60) -> tuple[CollectionRegistry, list[str], _Clock]:
    opened: list[str] = []
    clock = _Clock()

    async def open_service(user_id: str) -> Any:
        await asyncio.sleep(0)
        opened.append(user_id)
        return _FakeService(user_id)

    return CollectionRegistry(open_service, max_open, idle, clock), opened, clock


async def _use(registry: CollectionRegistry, user_id: str) -> Any:
    async with registry.lease(user_id) as service:
        return service


def test_registry_opens_once_and_reuses() -> None:
    async def scenario() -> None:
        registry, opened, _ = _registry()
        first, second = await asyncio.gather(_use(registry, "a"), _use(registry, "a"))
        assert first is second
        assert opened == ["a"]
        assert registry.open_count == 1

    asyncio.run(scenario())


def test_registry_evicts_least_recently_used_at_cap() -> None:
    async def scenario() -> None:
        registry, opened, clock = _registry(max_open=2)
        a = await _use(registry, "a")
        clock.now = 1
        await _use(registry, "b")
        clock.now = 2
        await _use(registry, "a")
        clock.now = 3
        await _use(registry, "c")
        assert opened == ["a", "b", "c"]
        assert registry.open_count == 2
        assert registry.evictions == 1
        assert not a.closed

    asyncio.run(scenario())


def test_registry_refuses_when_every_collection_is_busy() -> None:
    async def scenario() -> None:
        registry, _, _ = _registry(max_open=1)
        async with registry.lease("a"):
            with pytest.raises(ResourceLimitError):
                await _use(registry, "b")
        assert registry.open_count == 1

    asyncio.run(scenario())


def test_registry_keeps_collections_with_media_sync_running() -> None:
    async def scenario() -> None:
        registry, _, clock = _registry(max_open=1, idle=10)
        a = await _use(registry, "a")
        a.syncing_media = True
        clock.now = 100
        await registry.close_idle()
        assert not a.closed
        with pytest.raises(ResourceLimitError):
            await _use(registry, "b")
        a.syncing_media = False
        await registry.close_idle()
        assert a.closed
        assert registry.open_count == 0

    asyncio.run(scenario())


def test_registry_closes_only_idle_collections() -> None:
    async def scenario() -> None:
        registry, opened, clock = _registry(idle=10)
        a = await _use(registry, "a")
        clock.now = 5
        b = await _use(registry, "b")
        clock.now = 12
        await registry.close_idle()
        assert a.closed
        assert not b.closed
        await _use(registry, "a")
        assert opened == ["a", "b", "a"]
        await registry.close_all()
        assert registry.open_count == 0

    asyncio.run(scenario())


def test_failed_open_releases_its_slot() -> None:
    async def scenario() -> None:
        async def broken(_: str) -> Any:
            raise RuntimeError("cannot open")

        registry = CollectionRegistry(broken, 1, 60)
        with pytest.raises(RuntimeError):
            await _use(registry, "a")
        assert registry.open_count == 0

    asyncio.run(scenario())


@pytest.fixture
def users_file(tmp_path: Path) -> Path:
    return tmp_path / "data" / "users.json"


@pytest.fixture
def multi_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    sign_in_users(monkeypatch, tmp_path / "data", {"alice": "alice-token", "bob": "bob-token"})
    monkeypatch.setenv("ANKI_SYNC_ON_WRITE", "false")
    monkeypatch.setenv("ANKI_INITIAL_MEDIA_SYNC", "false")
    monkeypatch.setenv("ANKI_MEDIA_ROOT", str(tmp_path / "hdd" / "media"))
    monkeypatch.setenv("ANKI_BACKUP_ROOT", str(tmp_path / "hdd" / "backups"))
    monkeypatch.setenv("MCP_SCOPES", "read,write,admin,destructive")
    monkeypatch.setenv("ANKI_ALLOW_DESTRUCTIVE", "true")
    return Settings(_env_file=None)


class _Session:
    def __init__(self, client: TestClient, token: str) -> None:
        self.client = client
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        response = client.post(
            "/mcp",
            headers=self.headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        self.status_code = response.status_code
        if response.status_code == 200:
            self.headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        self.next_id = 2

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        response = self.client.post(
            "/mcp",
            headers=self.headers,
            json={
                "jsonrpc": "2.0",
                "id": self.next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        result = response.json()["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return {"error": json.loads(text[text.index("{") :])}
        return json.loads(text)


def _deck_names(session: _Session) -> set[str]:
    return {deck["name"] for deck in session.call("anki_decks_list", {})["items"]}


def test_each_user_sees_only_their_own_collection(
    multi_settings: Settings, users_file: Path, tmp_path: Path
) -> None:
    with TestClient(create_app(multi_settings)) as client:
        alice, bob = _Session(client, "alice-token"), _Session(client, "bob-token")
        alice.call("anki_decks_create", {"name": "Alice only"})
        bob.call("anki_decks_create", {"name": "Bob only"})
        assert "Alice only" in _deck_names(alice)
        assert "Bob only" not in _deck_names(alice)
        assert "Alice only" not in _deck_names(bob)

        content = base64.b64encode(b"GIF89a").decode()
        alice.call("anki_media_store", {"filename": "a.gif", "content_base64": content})
        assert (tmp_path / "hdd" / "media" / "alice" / "a.gif").read_bytes() == b"GIF89a"
        assert (tmp_path / "data" / "users" / "alice" / "collection.media").is_symlink()

        deck_id = next(
            d["id"] for d in alice.call("anki_decks_list", {})["items"] if d["name"] == "Alice only"
        )
        preview = alice.call("anki_decks_delete_preview", {"deck_id": deck_id})
        stolen = bob.call(
            "anki_decks_delete",
            {"deck_id": deck_id, "confirmation_token": preview["confirmation_token"]},
        )
        # Bob's call runs against Bob's own collection, which has no such deck.
        assert stolen["error"]["code"] == "NOT_FOUND"
        alice.call(
            "anki_decks_delete",
            {"deck_id": deck_id, "confirmation_token": preview["confirmation_token"]},
        )
        assert "Alice only" not in _deck_names(alice)
        assert list((tmp_path / "hdd" / "backups" / "alice").glob("*.colpkg"))

        ready = client.get("/health/ready").json()
        assert ready["open_collections"] == 2

        assert _Session(client, "not-a-token").status_code == 401
        users.main(["--file", str(users_file), "remove", "bob"])
        assert _Session(client, "bob-token").status_code == 401
    assert (tmp_path / "data" / "users" / "bob" / "collection.anki2").exists()


def test_users_file_rejects_unsafe_user_ids(tmp_path: Path) -> None:
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"users": {"../escape": {}}}))
    with pytest.raises(ValueError, match="invalid user id"):
        users.UserStore(path)
    with pytest.raises(SystemExit):
        users.main(["--file", str(path.with_name("new.json")), "add", "Bad/Id", "--hash", "x"])


def test_user_command_manages_accounts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "users.json"
    first, second = oauth.hash_password("first password"), oauth.hash_password("second pass")
    users.main(["--file", str(path), "add", "alice", "--hash", first, "--sync-username", "a"])
    users.main(["--file", str(path), "add", "bob", "--hash", first])
    with pytest.raises(SystemExit):
        users.main(["--file", str(path), "add", "alice", "--hash", first])
    with pytest.raises(SystemExit):
        users.main(["--file", str(path), "passwd", "alice", "--hash", "not-a-hash"])
    users.main(["--file", str(path), "passwd", "alice", "--hash", second])
    users.main(["--file", str(path), "remove", "bob"])
    capsys.readouterr()
    users.main(["--file", str(path), "list"])
    assert capsys.readouterr().out.split() == ["alice"]
    alice = users.UserStore(path).get("alice")
    assert alice is not None
    assert alice.password_hash == second
    assert alice.sync_username == "a"
    assert path.stat().st_mode & 0o077 == 0


def test_media_link_never_replaces_an_existing_folder(tmp_path: Path) -> None:
    collection = tmp_path / "collection.anki2"
    (tmp_path / "collection.media").mkdir()
    with pytest.raises(RuntimeError, match="is a directory"):
        _link_media_folder(collection, tmp_path / "elsewhere")
    (tmp_path / "collection.media").rmdir()
    _link_media_folder(collection, tmp_path / "elsewhere")
    _link_media_folder(collection, tmp_path / "elsewhere")
    with pytest.raises(RuntimeError, match="links to"):
        _link_media_folder(collection, tmp_path / "other")


def _oauth_token(client: TestClient, username: str, password: str) -> tuple[int, str | None]:
    registered = client.post("/register", json={"redirect_uris": [REDIRECT]}).json()
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    location = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s",
        },
    ).headers["location"]
    login = urlsplit(location)
    request_id = parse_qs(login.query)["request"][0]
    response = client.post(
        login.path, data={"request": request_id, "username": username, "password": password}
    )
    if response.status_code != 303:
        return response.status_code, None
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    tokens = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "client_secret": registered["client_secret"],
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    ).json()
    return 303, tokens["access_token"]


@pytest.fixture
def oauth_client(
    multi_settings: Settings, users_file: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(oauth, "_FAILED_LOGIN_DELAY_SECONDS", 0)
    with TestClient(create_app(Settings(_env_file=None)), follow_redirects=False) as client:
        yield client


def test_oauth_tokens_carry_the_signed_in_user(oauth_client: TestClient, users_file: Path) -> None:
    assert _oauth_token(oauth_client, "alice", "wrong")[0] == 401
    assert _oauth_token(oauth_client, "nobody", PASSWORD)[0] == 401
    _, alice_token = _oauth_token(oauth_client, "alice", PASSWORD)
    _, bob_token = _oauth_token(oauth_client, "bob", PASSWORD)
    assert alice_token is not None and bob_token is not None
    alice, bob = _Session(oauth_client, alice_token), _Session(oauth_client, bob_token)
    alice.call("anki_decks_create", {"name": "Signed in as Alice"})
    assert "Signed in as Alice" not in _deck_names(bob)

    stored = json.loads(users_file.read_text())
    del stored["users"]["alice"]
    users_file.write_text(json.dumps(stored))
    assert _Session(oauth_client, alice_token).status_code == 401


def test_background_work_does_not_keep_a_collection_open() -> None:
    async def scenario() -> None:
        registry, _, clock = _registry(idle=10)
        a = await _use(registry, "a")
        seen: list[str] = []

        async def work(service: Any) -> None:
            seen.append(service.user_id)
            clock.now = 100
            await registry.close_idle()
            assert not service.closed

        await registry.for_each_open(work)
        assert seen == ["a"]
        await registry.close_idle()
        assert a.closed

    asyncio.run(scenario())
