from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import pytest
from anki.collection import Collection
from conftest import owner_password_hash
from starlette.testclient import TestClient

from anki_mcp.app import create_app
from anki_mcp.config import Settings
from anki_mcp.migrate import migrate_legacy_install
from anki_mcp.users import UserStore


def _settings(data_dir: Path) -> Settings:
    return Settings(_env_file=None, ANKI_DATA_DIR=str(data_dir), MCP_PUBLIC_URL="http://localhost")


def _legacy_install(data_dir: Path, token: str) -> None:
    data_dir.mkdir(parents=True)
    collection = Collection(str(data_dir / "collection.anki2"))
    collection.decks.id("Before the upgrade")
    collection.close()
    (data_dir / "collection.media" / "a.gif").write_bytes(b"GIF89a")
    (data_dir / "state").mkdir()
    (data_dir / "state" / "sync-auth").write_text('{"hkey":"old-key"}')
    (data_dir / "backups").mkdir()
    (data_dir / "backups" / "old.colpkg").write_bytes(b"backup")
    access = {
        hashlib.sha256(token.encode()).hexdigest(): {
            "client_id": "claude",
            "scopes": ["read", "write", "admin"],
            "expires_at": int(time.time()) + 3600,
            "refresh_key": "r",
        }
    }
    (data_dir / "state" / "oauth.json").write_text(
        json.dumps({"clients": {}, "access": access, "refresh": {}})
    )


def test_old_install_moves_into_owner_and_keeps_working(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = tmp_path / "data"
    _legacy_install(data_dir, "old-oauth-token")
    environ = {
        "MCP_AUTH_MODE": "builtin",
        "MCP_OAUTH_PASSWORD_HASH": owner_password_hash(),
        "ANKI_SYNC_USERNAME": "me@example.com",
        "ANKI_SYNC_HOST": "https://sync.example.test/",
    }
    settings = _settings(data_dir)

    with caplog.at_level(logging.WARNING):
        migrate_legacy_install(settings, environ)

    owner_dir = data_dir / "users" / "owner"
    assert (owner_dir / "collection.anki2").exists()
    assert (owner_dir / "collection.media" / "a.gif").read_bytes() == b"GIF89a"
    assert (owner_dir / "state" / "sync-auth").read_text() == '{"hkey":"old-key"}'
    assert (owner_dir / "backups" / "old.colpkg").exists()
    assert not (data_dir / "collection.anki2").exists()
    assert not (owner_dir / "state" / "oauth.json").exists()
    owner = UserStore(settings.users_file).get("owner")
    assert owner is not None
    assert owner.password_hash == owner_password_hash()
    assert owner.sync_username == "me@example.com"
    assert owner.sync_endpoint == "https://sync.example.test/"
    assert "MCP_AUTH_MODE" in caplog.text
    assert "passwd" not in caplog.text

    with TestClient(create_app(settings)) as client:
        headers = {
            "Authorization": "Bearer old-oauth-token",
            "Accept": "application/json, text/event-stream",
        }
        initialized = client.post(
            "/mcp",
            headers=headers,
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
        assert initialized.status_code == 200
        headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
        decks = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "anki_decks_list", "arguments": {}},
            },
        ).json()
    assert "Before the upgrade" in decks["result"]["content"][0]["text"]


def test_bearer_only_install_is_told_to_set_a_password(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = tmp_path / "data"
    _legacy_install(data_dir, "unused")
    with caplog.at_level(logging.WARNING):
        migrate_legacy_install(_settings(data_dir), {"MCP_AUTH_TOKEN": "old-bearer"})
    assert "passwd owner" in caplog.text
    assert "MCP_AUTH_TOKEN" in caplog.text
    assert "old-bearer" not in caplog.text


def test_fresh_install_starts_with_an_empty_users_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "data")
    migrate_legacy_install(settings, {})
    assert json.loads(settings.users_file.read_text()) == {"users": {}}
    assert settings.users_file.stat().st_mode & 0o077 == 0


def test_migration_refuses_to_merge_two_layouts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _legacy_install(data_dir, "unused")
    (data_dir / "users" / "owner").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="remove one of them"):
        migrate_legacy_install(_settings(data_dir), {})
    assert (data_dir / "collection.anki2").exists()


def test_interrupted_migration_needs_a_person(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "users" / ".owner-migrating").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="stopped part way"):
        migrate_legacy_install(_settings(data_dir), {})
