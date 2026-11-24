"""Built-in OAuth authorization server.

Every MCP client signs in as a user from the users file, with that user's password, and the MCP
SDK provides the OAuth endpoints. Also provides the anki-mcp-hash-password command.
"""

from __future__ import annotations

import asyncio
import base64
import getpass
import hashlib
import hmac
import html
import logging
import secrets
import sys
import time
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
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from anki_mcp.config import Settings
from anki_mcp.pages import page
from anki_mcp.state import PersistentState
from anki_mcp.users import UserStore

_LOGGER = logging.getLogger(__name__)
_ACCESS_TOKEN_SECONDS = 3600
_REFRESH_TOKEN_SECONDS = 30 * 24 * 3600
_AUTHORIZATION_CODE_SECONDS = 300
_LOGIN_REQUEST_SECONDS = 600
_FAILED_LOGIN_DELAY_SECONDS = 1.0
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


def _is_loopback_redirect(uri: str) -> bool:
    """RFC 8252 section 7.3: native clients redirect to a loopback port they pick at runtime."""
    parts = urlsplit(uri)
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        port is not None
        and parts.scheme == "http"
        and parts.hostname in {"localhost", "127.0.0.1", "::1"}
        and parts.username is None
        and parts.password is None
        and not parts.fragment
    )


class _Code(AuthorizationCode):
    subject: str | None = None


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class BuiltinOAuthProvider:
    """OAuth authorization server: password login, DCR, rotating refresh tokens.

    People sign in with a user id and password, and every token carries that user id as its
    subject. Tokens are persisted only as SHA-256 hashes, so a leaked state file grants nothing.
    """

    def __init__(self, settings: Settings, users: UserStore) -> None:
        self._users = users
        # Unknown user ids are checked against this so a failed login costs the same either way.
        self._decoy_hash = hash_password(secrets.token_urlsafe(32))
        self._public_url = settings.public_url
        self._redirect_uris = settings.oauth_redirect_uris
        self.scopes = sorted(settings.scopes)
        self._path = settings.data_dir / "oauth.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        stored = PersistentState._load_json(self._path) or {}
        self._clients: dict[str, dict[str, Any]] = stored.get("clients", {})
        self._access: dict[str, dict[str, Any]] = stored.get("access", {})
        self._refresh: dict[str, dict[str, Any]] = stored.get("refresh", {})
        self._codes: dict[str, _Code] = {}
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
        refused = {uri for uri in requested - self._redirect_uris if not _is_loopback_redirect(uri)}
        if not requested or refused:
            _LOGGER.debug(
                "refused client registration: redirect_uris %r not loopback or in "
                "MCP_OAUTH_REDIRECT_URIS",
                sorted(uri[:200] for uri in refused),
            )
            raise RegistrationError(
                "invalid_redirect_uri",
                "redirect_uris must be loopback or listed in MCP_OAUTH_REDIRECT_URIS",
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
        code = self._codes.pop(authorization_code.code, None)
        if code is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(code.client_id, code.scopes, code.subject)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = self._refresh.get(_token_key(refresh_token))
        if stored is None or stored["client_id"] != client.client_id:
            return None
        if stored["expires_at"] <= time.time() or not self._subject_allowed(stored):
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
        return self._issue(refresh_token.client_id, scopes, stored.get("subject"))

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = self._access.get(_token_key(token))
        if stored is None or stored["expires_at"] <= time.time():
            return None
        if not self._subject_allowed(stored):
            return None
        return AccessToken(
            token=token,
            client_id=stored["client_id"],
            scopes=stored["scopes"],
            expires_at=stored["expires_at"],
            subject=stored.get("subject"),
        )

    def _subject_allowed(self, stored: dict[str, Any]) -> bool:
        """A token is only good while its user still exists."""
        subject = stored.get("subject")
        return subject is not None and self._users.get(subject) is not None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        key = _token_key(token.token)
        access = self._access.pop(key, None)
        refresh = self._refresh.pop(key, None)
        if access is not None:
            self._refresh.pop(access["refresh_key"], None)
        if refresh is not None:
            self._access.pop(refresh["access_key"], None)
        self._save()

    def _issue(self, client_id: str, scopes: list[str], subject: str | None) -> OAuthToken:
        now = int(time.time())
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        access_key, refresh_key = _token_key(access_token), _token_key(refresh_token)
        self._access[access_key] = {
            "client_id": client_id,
            "scopes": scopes,
            "expires_at": now + _ACCESS_TOKEN_SECONDS,
            "refresh_key": refresh_key,
            "subject": subject,
        }
        self._refresh[refresh_key] = {
            "client_id": client_id,
            "scopes": scopes,
            "expires_at": now + _REFRESH_TOKEN_SECONDS,
            "access_key": access_key,
            "subject": subject,
        }
        self._save()
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )

    def _login_page(
        self, request_id: str, message: str = "", status: int = 200, username: str = ""
    ) -> Response:
        pending = self._pending.get(request_id)
        if pending is None or pending[0] <= time.time():
            return page(
                "Sign-in expired",
                "<h1>Sign-in expired</h1><p>This sign-in request has expired. "
                "Start again from your app.</p>",
                400,
            )
        _, client_id, params = pending
        client = self._clients.get(client_id, {})
        name = html.escape(str(client.get("client_name") or client_id))
        target = html.escape(urlsplit(str(params.redirect_uri)).netloc)
        error = f"<p class='alert' role='alert'>{html.escape(message)}</p>" if message else ""
        return page(
            "Sign in",
            f"<h1>Sign in to Anki MCP</h1>"
            f"<p><strong>{name}</strong> is asking for access to your Anki collection.</p>"
            f"{error}<form method='post'>"
            f"<input type='hidden' name='request' value='{html.escape(request_id)}'>"
            f"<label>User <input name='username' value='{html.escape(username)}' "
            f"autocomplete='username' autocapitalize='none' spellcheck='false' required "
            f"{'' if username else 'autofocus'}></label>"
            f"<label>Password <input type='password' name='password' required "
            f"autocomplete='current-password' {'autofocus' if username else ''}></label>"
            f"<button type='submit'>Allow access</button></form>"
            f"<p class='muted after'>You will be sent back to {target}.</p>",
            status,
        )

    async def login(self, request: Request) -> Response:
        if request.method == "GET":
            return self._login_page(request.query_params.get("request", ""))
        form = await request.form()
        request_id = str(form.get("request", ""))
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        user = self._users.get(username)
        stored_hash = user.password_hash if user is not None else None
        known = stored_hash is not None
        password_hash = stored_hash or self._decoy_hash
        setup_unfinished = user is not None and not user.can_sign_in
        async with self._login_lock:
            pending = self._pending.get(request_id)
            if pending is None or pending[0] <= time.time():
                return self._login_page(request_id)
            valid = await asyncio.to_thread(verify_password, password, password_hash)
            if not (valid and known):
                await asyncio.sleep(_FAILED_LOGIN_DELAY_SECONDS)
                return self._login_page(request_id, "Wrong user or password.", 401, username)
            if setup_unfinished:
                return self._login_page(
                    request_id,
                    "Your account isn't ready yet. Finish setup from the link in your email, "
                    "then try again.",
                    403,
                    username,
                )
            _, client_id, params = self._pending.pop(request_id)
        subject = username
        code = secrets.token_urlsafe(32)
        self._codes[code] = _Code(
            subject=subject,
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


def main() -> None:
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    if len(password) < 12:
        sys.exit("Use at least 12 characters.")
    print(hash_password(password))
