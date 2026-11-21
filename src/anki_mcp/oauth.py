from __future__ import annotations

import asyncio
import base64
import getpass
import hashlib
import hmac
import html
import secrets
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from anki_mcp.config import Settings
from anki_mcp.state import PersistentState

_ACCESS_TOKEN_SECONDS = 3600
_REFRESH_TOKEN_SECONDS = 30 * 24 * 3600
_AUTHORIZATION_CODE_SECONDS = 300
_LOGIN_REQUEST_SECONDS = 600
_FAILED_LOGIN_DELAY_SECONDS = 1.0
# ponytail: hard cap on dynamically registered clients; add eviction if real clients hit it
_MAX_CLIENTS = 50
_MAX_PENDING_LOGINS = 100
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_LOGIN_PATH = "/oauth/login"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
    )
    encoded_salt = base64.b64encode(salt).decode()
    encoded_digest = base64.b64encode(digest).decode()
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${encoded_salt}${encoded_digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest, base64.b64decode(expected))


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class BuiltinOAuthProvider:
    """Single-owner OAuth authorization server: password login, DCR, rotating refresh tokens.

    Tokens are persisted only as SHA-256 hashes, so a leaked state file grants nothing.
    """

    def __init__(self, settings: Settings) -> None:
        assert settings.oauth_password_hash is not None
        self._password_hash = settings.oauth_password_hash.get_secret_value()
        self._public_url = settings.public_url
        self._redirect_uris = settings.oauth_redirect_uris
        self.scopes = sorted(settings.scopes)
        self._path = Path(settings.collection_path).parent / "state" / "oauth.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        stored = PersistentState._load_json(self._path) or {}
        self._clients: dict[str, dict[str, Any]] = stored.get("clients", {})
        self._access: dict[str, dict[str, Any]] = stored.get("access", {})
        self._refresh: dict[str, dict[str, Any]] = stored.get("refresh", {})
        self._codes: dict[str, AuthorizationCode] = {}
        self._pending: dict[str, tuple[float, str, AuthorizationParams]] = {}
        self._login_lock = asyncio.Lock()

    def _save(self) -> None:
        now = time.time()
        self._access = {k: v for k, v in self._access.items() if v["expires_at"] > now}
        self._refresh = {k: v for k, v in self._refresh.items() if v["expires_at"] > now}
        PersistentState._atomic_json(
            self._path,
            {"clients": self._clients, "access": self._access, "refresh": self._refresh},
            secret=True,
        )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        stored = self._clients.get(client_id)
        return OAuthClientInformationFull.model_validate(stored) if stored else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        requested = {str(uri) for uri in client_info.redirect_uris or []}
        if not requested or not requested <= self._redirect_uris:
            raise RegistrationError(
                "invalid_redirect_uri", "redirect_uris must be listed in MCP_OAUTH_REDIRECT_URIS"
            )
        if len(self._clients) >= _MAX_CLIENTS:
            raise RegistrationError("invalid_client_metadata", "client registration limit reached")
        assert client_info.client_id is not None
        self._clients[client_info.client_id] = client_info.model_dump(mode="json")
        self._save()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v[0] > now}
        if len(self._pending) >= _MAX_PENDING_LOGINS:
            raise AuthorizeError("temporarily_unavailable", "too many pending sign-in requests")
        request_id = secrets.token_urlsafe(32)
        assert client.client_id is not None
        self._pending[request_id] = (now + _LOGIN_REQUEST_SECONDS, client.client_id, params)
        return f"{self._public_url}{_LOGIN_PATH}?request={request_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(authorization_code.client_id, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = self._refresh.get(_token_key(refresh_token))
        if stored is None or stored["client_id"] != client.client_id:
            return None
        if stored["expires_at"] <= time.time():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=stored["client_id"],
            scopes=stored["scopes"],
            expires_at=stored["expires_at"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        stored = self._refresh.pop(_token_key(refresh_token.token), None)
        if stored is None:
            raise TokenError("invalid_grant", "refresh token already used")
        self._access.pop(stored["access_key"], None)
        return self._issue(refresh_token.client_id, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = self._access.get(_token_key(token))
        if stored is None or stored["expires_at"] <= time.time():
            return None
        return AccessToken(
            token=token,
            client_id=stored["client_id"],
            scopes=stored["scopes"],
            expires_at=stored["expires_at"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        key = _token_key(token.token)
        access = self._access.pop(key, None)
        refresh = self._refresh.pop(key, None)
        if access is not None:
            self._refresh.pop(access["refresh_key"], None)
        if refresh is not None:
            self._access.pop(refresh["access_key"], None)
        self._save()

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = int(time.time())
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        access_key, refresh_key = _token_key(access_token), _token_key(refresh_token)
        self._access[access_key] = {
            "client_id": client_id,
            "scopes": scopes,
            "expires_at": now + _ACCESS_TOKEN_SECONDS,
            "refresh_key": refresh_key,
        }
        self._refresh[refresh_key] = {
            "client_id": client_id,
            "scopes": scopes,
            "expires_at": now + _REFRESH_TOKEN_SECONDS,
            "access_key": access_key,
        }
        self._save()
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )

    def _login_page(self, request_id: str, message: str = "", status: int = 200) -> Response:
        pending = self._pending.get(request_id)
        if pending is None or pending[0] <= time.time():
            return _html("<p>This sign-in request has expired. Start again from your app.</p>", 400)
        _, client_id, params = pending
        client = self._clients.get(client_id, {})
        name = html.escape(str(client.get("client_name") or client_id))
        target = html.escape(urlsplit(str(params.redirect_uri)).netloc)
        error = f"<p role='alert'><strong>{html.escape(message)}</strong></p>" if message else ""
        return _html(
            f"<h1>Sign in to Anki MCP</h1>"
            f"<p><strong>{name}</strong> is asking for access to your Anki collection. "
            f"You will be sent back to <strong>{target}</strong>.</p>{error}"
            f"<form method='post'>"
            f"<input type='hidden' name='request' value='{html.escape(request_id)}'>"
            f"<label>Password <input type='password' name='password' autofocus required "
            f"autocomplete='current-password'></label> <button type='submit'>Allow</button>"
            f"</form>",
            status,
        )

    async def login(self, request: Request) -> Response:
        if request.method == "GET":
            return self._login_page(request.query_params.get("request", ""))
        form = await request.form()
        request_id = str(form.get("request", ""))
        password = str(form.get("password", ""))
        async with self._login_lock:
            pending = self._pending.get(request_id)
            if pending is None or pending[0] <= time.time():
                return self._login_page(request_id)
            valid = await asyncio.to_thread(verify_password, password, self._password_hash)
            if not valid:
                await asyncio.sleep(_FAILED_LOGIN_DELAY_SECONDS)
                return self._login_page(request_id, "Wrong password.", 401)
            _, client_id, params = self._pending.pop(request_id)
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or self.scopes,
            expires_at=time.time() + _AUTHORIZATION_CODE_SECONDS,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=303,
        )

    def routes(self) -> list[Route]:
        return [Route(_LOGIN_PATH, self.login, methods=["GET", "POST"])]


def _html(body: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Anki MCP</title></head><body>{body}</body></html>",
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
        },
    )


def main() -> None:
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    if len(password) < 12:
        sys.exit("Use at least 12 characters.")
    print(hash_password(password))
