from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from anki.collection import Collection
from anki.errors import NetworkError, SyncError, SyncErrorKind
from anki.sync import SyncAuth
from conftest import OWNER_PASSWORD, owner_password_hash
from pydantic import ValidationError
from starlette.testclient import TestClient

from anki_mcp import oauth, registration, users
from anki_mcp.app import create_app
from anki_mcp.collection import AnkiCollectionService
from anki_mcp.config import Settings

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
NEW_PASSWORD = "a brand new password"


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "users.json").write_text(json.dumps({"users": {}}))
    monkeypatch.setenv("ANKI_DATA_DIR", str(data))
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://localhost")
    monkeypatch.setenv("ANKI_SYNC_ON_WRITE", "false")
    monkeypatch.setattr(oauth, "_FAILED_LOGIN_DELAY_SECONDS", 0)
    monkeypatch.setattr(registration, "_POLL_SECONDS", 0.01)
    return data


def _users_file(data_dir: Path) -> Path:
    return data_dir / "users.json"


def _entry(data_dir: Path, user_id: str) -> dict[str, Any]:
    return json.loads(_users_file(data_dir).read_text())["users"][user_id]


def _invite(data_dir: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> str:
    users.main(["--file", str(_users_file(data_dir)), "invite", "alice", *extra])
    out = capsys.readouterr().out
    match = re.search(r"http://localhost/setup\?token=(\S+)", out)
    assert match, out
    return match.group(1)


def _path(token: str) -> str:
    return f"/setup?token={token}"


def _redirect_token(response: Any) -> str:
    assert response.status_code == 303, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["token"][0]


def _sign_in_status(client: TestClient, username: str, password: str) -> int:
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
        },
    ).headers["location"]
    login = urlsplit(location)
    request_id = parse_qs(login.query)["request"][0]
    return client.post(
        login.path, data={"request": request_id, "username": username, "password": password}
    ).status_code


class _FirstSync:
    """Stands in for AnkiWeb: a download that waits to be released, then a media sync."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.download_released = threading.Event()
        self.media_done = threading.Event()
        self.fail_download_with: Exception | None = None
        self.logins: list[tuple[str, str]] = []
        test = self

        def sync_login(self: Collection, username: str, password: str, endpoint: Any) -> Any:
            test.logins.append((username, password))
            if password != "ankiweb-password":
                raise SyncError("bad login", None, None, None, SyncErrorKind.AUTH)
            return SyncAuth(hkey="host-key", endpoint=endpoint)

        async def bootstrap(self: AnkiCollectionService, *args: Any) -> dict[str, Any]:
            if test.fail_download_with is not None:
                error, test.fail_download_with = test.fail_download_with, None
                raise error
            await _wait(test.download_released)
            return {"bootstrapped": True}

        async def progress(self: AnkiCollectionService) -> tuple[int, int]:
            return 6_000_000, 10_000_000

        async def status(self: AnkiCollectionService) -> dict[str, Any]:
            done = test.media_done.is_set()
            return {
                "last_media_sync_at": "2026-11-24T00:00:00+00:00" if done else None,
                "media_sync": {
                    "state": "completed" if done else "in_progress",
                    "in_progress": not done,
                    "error": None,
                    "checked": "Checked: 40",
                    "added": "Added: 12",
                    "removed": "",
                },
            }

        monkeypatch.setattr(Collection, "sync_login", sync_login)
        monkeypatch.setattr(AnkiCollectionService, "bootstrap", bootstrap)
        monkeypatch.setattr(AnkiCollectionService, "full_sync_progress", progress)
        monkeypatch.setattr(AnkiCollectionService, "status", status)


async def _wait(event: threading.Event) -> None:
    import asyncio

    await asyncio.to_thread(event.wait, 10)


def _until(client: TestClient, token: str, text: str) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body = client.get(_path(token)).text
        if text in body:
            return body
        time.sleep(0.02)
    raise AssertionError(f"never saw {text!r}; last page:\n{body}")


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(_env_file=None)), follow_redirects=False) as test_client:
        yield test_client


def test_invited_user_sets_a_password_signs_in_to_ankiweb_and_waits_for_media(
    data_dir: Path,
    client: TestClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_sync = _FirstSync(monkeypatch)
    token = _invite(data_dir, capsys, "--email", "alice@example.com")
    assert _entry(data_dir, "alice")["setup"] == "password"
    assert token not in _users_file(data_dir).read_text()
    assert _sign_in_status(client, "alice", NEW_PASSWORD) == 401

    page = client.get(_path(token))
    assert "Step 1 of 3" in page.text
    short = client.post("/setup", data={"token": token, "password": "short", "repeat": "short"})
    assert short.status_code == 400
    mismatch = client.post(
        "/setup", data={"token": token, "password": NEW_PASSWORD, "repeat": NEW_PASSWORD + "!"}
    )
    assert mismatch.status_code == 400
    step2 = _redirect_token(
        client.post(
            "/setup", data={"token": token, "password": NEW_PASSWORD, "repeat": NEW_PASSWORD}
        )
    )
    assert client.get(_path(token)).status_code == 400
    assert _sign_in_status(client, "alice", NEW_PASSWORD) == 403

    assert "Step 2 of 3" in client.get(_path(step2)).text
    wrong = client.post(
        "/setup", data={"token": step2, "username": "alice@ankiweb", "password": "nope"}
    )
    assert wrong.status_code == 401
    assert "didn" in wrong.text
    started = client.post(
        "/setup",
        data={"token": step2, "username": "alice@ankiweb", "password": "ankiweb-password"},
    )
    assert _redirect_token(started) == step2
    entry = _entry(data_dir, "alice")
    assert entry["sync_username"] == "alice@ankiweb"
    assert "ankiweb-password" not in _users_file(data_dir).read_text()

    downloading = _until(client, step2, "Downloading your cards")
    assert "value='6000000' max='10000000'" in downloading
    assert "60%" in downloading
    assert "http-equiv='refresh'" in downloading
    assert _sign_in_status(client, "alice", NEW_PASSWORD) == 403

    first_sync.download_released.set()
    media = _until(client, step2, "downloading media")
    assert "Added: 12" in media
    assert _sign_in_status(client, "alice", NEW_PASSWORD) == 403

    first_sync.media_done.set()
    done = _until(client, step2, "all set")
    assert "http://localhost/mcp" in done
    assert client.get(_path(step2)).status_code == 400
    assert "setup" not in _entry(data_dir, "alice")
    assert _sign_in_status(client, "alice", NEW_PASSWORD) == 303


def test_a_failed_copy_can_be_retried(
    data_dir: Path,
    client: TestClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_sync = _FirstSync(monkeypatch)
    first_sync.fail_download_with = NetworkError("secret detail", None, None, None)
    token = _invite(data_dir, capsys, "--email", "alice@example.com")
    step2 = _redirect_token(
        client.post(
            "/setup", data={"token": token, "password": NEW_PASSWORD, "repeat": NEW_PASSWORD}
        )
    )
    client.post(
        "/setup",
        data={"token": step2, "username": "alice@ankiweb", "password": "ankiweb-password"},
    )
    failed = _until(client, step2, "Try again")
    assert "could not be reached" in failed
    assert "secret detail" not in failed

    first_sync.download_released.set()
    first_sync.media_done.set()
    assert _redirect_token(client.post("/setup", data={"token": step2})) == step2
    _until(client, step2, "all set")


def test_reset_link_changes_only_the_password(
    data_dir: Path, client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    _users_file(data_dir).write_text(
        json.dumps(
            {"users": {"alice": {"password_hash": owner_password_hash(), "email": "a@b.example"}}}
        )
    )
    users.main(["--file", str(_users_file(data_dir)), "reset", "alice"])
    token = re.search(r"token=(\S+)", capsys.readouterr().out).group(1)  # type: ignore[union-attr]
    assert _sign_in_status(client, "alice", OWNER_PASSWORD) == 303

    assert "Choose a new password" in client.get(_path(token)).text
    changed = client.post(
        "/setup", data={"token": token, "password": NEW_PASSWORD, "repeat": NEW_PASSWORD}
    )
    assert "Password changed" in changed.text
    assert client.get(_path(token)).status_code == 400
    assert "setup" not in _entry(data_dir, "alice")
    assert _sign_in_status(client, "alice", OWNER_PASSWORD) == 401
    assert _sign_in_status(client, "alice", NEW_PASSWORD) == 303


def test_setup_links_expire(
    data_dir: Path, client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    token = _invite(data_dir, capsys, "--email", "alice@example.com")
    raw = json.loads(_users_file(data_dir).read_text())
    raw["users"]["alice"]["setup_expires_at"] = time.time() - 1
    _users_file(data_dir).write_text(json.dumps(raw))
    assert client.get(_path(token)).status_code == 400
    assert client.get("/setup").status_code == 400


def test_invite_command_rules(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = str(_users_file(data_dir))
    with pytest.raises(SystemExit):
        users.main(["--file", path, "invite", "alice", "--email", "not an email"])
    with pytest.raises(SystemExit):
        users.main(["--file", path, "invite", "alice"])
    first = _invite(
        data_dir, capsys, "--email", "alice@example.com", "--sync-host", "https://s.test/"
    )
    assert _entry(data_dir, "alice")["sync_endpoint"] == "https://s.test/"
    with pytest.raises(SystemExit):
        users.main(["--file", path, "invite", "alice", "--email", "alice@example.com"])
    with pytest.raises(SystemExit):
        users.main(["--file", path, "reset", "alice"])
    second = _invite(data_dir, capsys, "--resend")
    assert second != first
    assert _entry(data_dir, "alice")["setup_sha256"] == hashlib.sha256(second.encode()).hexdigest()

    raw = json.loads(_users_file(data_dir).read_text())
    raw["users"]["alice"].pop("setup")
    raw["users"]["alice"]["password_hash"] = owner_password_hash()
    _users_file(data_dir).write_text(json.dumps(raw))
    with pytest.raises(SystemExit):
        users.main(["--file", path, "invite", "alice", "--resend"])


def test_invite_emails_the_link_when_smtp_is_set(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MCP_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("MCP_SMTP_FROM", "anki@example.com")
    sent: list[tuple[str, str, str, bool]] = []
    monkeypatch.setattr(
        registration,
        "send_setup_email",
        lambda settings, to, user_id, link, *, reset: sent.append((to, user_id, link, reset)),
    )
    users.main(["--file", str(_users_file(data_dir)), "invite", "alice", "--email", "a@b.example"])
    out = capsys.readouterr().out
    assert sent and sent[0][:2] == ("a@b.example", "alice")
    assert sent[0][2].startswith("http://localhost/setup?token=")
    assert sent[0][2] not in out


class _FakeSMTP:
    instances: list[_FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float, **_: Any) -> None:
        self.calls: list[Any] = [("connect", host, port)]
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *_: Any) -> None:
        self.calls.append("quit")

    def starttls(self, context: Any) -> None:
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))

    def send_message(self, message: Any) -> None:
        self.calls.append(("send", message["To"], message["Subject"], message.get_content()))


def test_setup_email_uses_starttls_and_login(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "smtp-password"
    secret.write_text("mail-secret\n")
    monkeypatch.setattr(registration.smtplib, "SMTP", _FakeSMTP)
    settings = Settings(
        _env_file=None,
        MCP_SMTP_HOST="smtp.example.com",
        MCP_SMTP_FROM="anki@example.com",
        MCP_SMTP_USERNAME="mailer",
        MCP_SMTP_PASSWORD_FILE=str(secret),
    )
    registration.send_setup_email(
        settings, "a@b.example", "alice", "http://localhost/setup?token=t", reset=False
    )
    calls = _FakeSMTP.instances[-1].calls
    assert calls[:3] == [
        ("connect", "smtp.example.com", 587),
        "starttls",
        ("login", "mailer", "mail-secret"),
    ]
    to, subject, body = calls[3][1:]
    assert to == "a@b.example"
    assert "invited" in subject
    assert "http://localhost/setup?token=t" in body


def test_smtp_settings_never_send_links_in_the_clear(data_dir: Path) -> None:
    with pytest.raises(ValidationError, match="MCP_SMTP_FROM"):
        Settings(_env_file=None, MCP_SMTP_HOST="smtp.example.com")
    with pytest.raises(ValidationError, match="loopback"):
        Settings(
            _env_file=None,
            MCP_SMTP_HOST="smtp.example.com",
            MCP_SMTP_FROM="a@b.example",
            MCP_SMTP_SECURITY="none",
        )
    assert (
        Settings(
            _env_file=None,
            MCP_SMTP_HOST="localhost",
            MCP_SMTP_FROM="a@b.example",
            MCP_SMTP_SECURITY="none",
        ).smtp_security
        == "none"
    )
