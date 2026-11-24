from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import OWNER_PASSWORD as PASSWORD
from conftest import use_owner
from pydantic import ValidationError
from starlette.testclient import TestClient

from anki_mcp import oauth
from anki_mcp.app import create_app
from anki_mcp.config import Settings

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1"},
    },
}


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    monkeypatch.setattr(oauth, "_FAILED_LOGIN_DELAY_SECONDS", 0)
    use_owner(monkeypatch, tmp_path, "unused-token")
    monkeypatch.setenv("ANKI_SYNC_ON_WRITE", "false")
    monkeypatch.setenv("MCP_SCOPES", "read,write")
    return Settings(_env_file=None)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), follow_redirects=False) as test_client:
        yield test_client


def _mcp(client: TestClient, token: str) -> int:
    return client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json=INITIALIZE,
    ).status_code


def _sign_in(request_id: str, password: str = PASSWORD) -> dict[str, str]:
    return {"request": request_id, "username": "owner", "password": password}


def _register(client: TestClient) -> tuple[str, str]:
    response = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude"})
    assert response.status_code == 201, response.text
    body = response.json()
    return body["client_id"], body["client_secret"]


def _login_url(client: TestClient, client_id: str, challenge: str) -> str:
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
    )
    assert response.status_code == 302, response.text
    return response.headers["location"]


def test_password_hash_round_trip() -> None:
    encoded = oauth.hash_password("secret-password")
    assert oauth.verify_password("secret-password", encoded)
    assert not oauth.verify_password("wrong", encoded)
    assert not oauth.verify_password("secret-password", "garbage")


def test_public_url_must_be_https_except_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://anki.example.com")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://anki.example.com")
    assert Settings(_env_file=None).public_url == "https://anki.example.com"
    monkeypatch.delenv("MCP_PUBLIC_URL")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:33418/callback",
        "http://127.0.0.1:51234/callback",
        "http://[::1]:8080/",
    ],
)
def test_loopback_redirect_registers_on_any_port(client: TestClient, uri: str) -> None:
    response = client.post("/register", json={"redirect_uris": [uri]})
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "uri",
    [
        "https://localhost:33418/callback",
        "http://localhost.evil.example/callback",
        "http://localhost@evil.example/callback",
        "http://user@localhost:33418/callback",
        "http://10.0.0.1:33418/callback",
        "http://localhost/callback",
    ],
)
def test_non_loopback_redirect_is_refused(client: TestClient, uri: str) -> None:
    response = client.post("/register", json={"redirect_uris": [REDIRECT, uri]})
    assert response.status_code == 400, response.text


def test_full_flow_and_rejections(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    assert client.post("/mcp", json=INITIALIZE).status_code == 401
    assert _mcp(client, "not-a-token") == 401
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"].rstrip("/") == "http://localhost"
    assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200

    with caplog.at_level(logging.DEBUG, logger="anki_mcp.oauth"):
        rogue = client.post("/register", json={"redirect_uris": ["https://evil.example/cb"]})
    assert rogue.status_code == 400
    assert "https://evil.example/cb" in caplog.text

    client_id, secret = _register(client)
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    login = urlsplit(_login_url(client, client_id, challenge))
    request_id = parse_qs(login.query)["request"][0]
    page = client.get(login.path, params={"request": request_id})
    assert page.status_code == 200
    assert "Claude" in page.text
    assert page.headers["x-frame-options"] == "DENY"

    wrong = client.post(login.path, data=_sign_in(request_id, "nope"))
    assert wrong.status_code == 401
    right = client.post(login.path, data=_sign_in(request_id))
    assert right.status_code == 303
    callback = urlsplit(right.headers["location"])
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == REDIRECT
    query = parse_qs(callback.query)
    assert query["state"] == ["xyz"]
    replay = client.post(login.path, data=_sign_in(request_id))
    assert replay.status_code == 400

    token_request = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": secret,
        "code": query["code"][0],
        "redirect_uri": REDIRECT,
        "code_verifier": "wrong" * 10,
    }
    assert client.post("/token", data=token_request).status_code == 400
    token_request["code_verifier"] = verifier
    tokens = client.post("/token", data=token_request).json()
    assert set(tokens["scope"].split()) == {"read", "write"}
    assert client.post("/token", data=token_request).status_code == 400
    assert _mcp(client, tokens["access_token"]) == 200

    refresh_request = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": secret,
        "refresh_token": tokens["refresh_token"],
    }
    refreshed = client.post("/token", data=refresh_request).json()
    assert _mcp(client, refreshed["access_token"]) == 200
    assert _mcp(client, tokens["access_token"]) == 401
    assert client.post("/token", data=refresh_request).status_code == 400

    client.post(
        "/revoke",
        data={
            "client_id": client_id,
            "client_secret": secret,
            "token": refreshed["refresh_token"],
        },
    )
    assert _mcp(client, refreshed["access_token"]) == 401


def test_tokens_survive_restart(settings: Settings) -> None:
    with TestClient(create_app(settings), follow_redirects=False) as first:
        client_id, secret = _register(first)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        login = urlsplit(_login_url(first, client_id, challenge))
        request_id = parse_qs(login.query)["request"][0]
        callback = first.post(login.path, data=_sign_in(request_id))
        code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
        tokens = first.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": secret,
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": verifier,
            },
        ).json()
    state_file = settings.data_dir / "oauth.json"
    assert tokens["access_token"] not in state_file.read_text()
    with TestClient(create_app(settings)) as second:
        assert _mcp(second, tokens["access_token"]) == 200


def test_sign_in_page_stylesheet_is_allowed_by_its_security_policy(client: TestClient) -> None:
    client_id, _ = _register(client)
    login = urlsplit(_login_url(client, client_id, "c" * 43))
    page = client.get(login.path, params={"request": parse_qs(login.query)["request"][0]})
    style = page.text.split("<style>", 1)[1].split("</style>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    policy = page.headers["content-security-policy"]
    assert f"style-src 'sha256-{digest}'" in policy
    assert "unsafe-inline" not in policy
    assert " style=" not in page.text
    assert "name='username'" in page.text
