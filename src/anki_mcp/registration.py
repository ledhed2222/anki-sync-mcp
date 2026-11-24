"""Invitations and self-service setup: set a password, sign in to AnkiWeb, download the collection.

An admin runs `anki-mcp-user invite`, which emails a one-time link to /setup. The person picks
their password, signs in to AnkiWeb (or the sync server named in the invite), and watches their
collection and media download. They can only sign in to MCP clients once that has finished.
`anki-mcp-user reset` emails a link that only changes the password.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import re
import secrets
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from anki.errors import NetworkError, SyncError, SyncErrorKind
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from anki_mcp.collection import AnkiCollectionService
from anki_mcp.config import Settings
from anki_mcp.pages import page
from anki_mcp.users import CollectionRegistry, User, UserStore, update_users_file

_LOGGER = logging.getLogger(__name__)
_SETUP_PATH = "/setup"
_MIN_PASSWORD = 12
_POLL_SECONDS = 2.0
_REFRESH_SECONDS = 2
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Stages whose link expires; a running or finished download keeps its link until it is shown.
_EXPIRING_STAGES = frozenset({"password", "ankiweb", "reset"})


def valid_email(email: str) -> bool:
    return len(email) <= 254 and _EMAIL.fullmatch(email) is not None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def begin_setup(entry: dict[str, Any], stage: str, hours: float) -> str:
    """Put a users-file entry at a setup stage with a fresh link; returns the link's token."""
    token = secrets.token_urlsafe(32)
    entry.update(
        setup=stage,
        setup_sha256=_token_hash(token),
        setup_expires_at=time.time() + hours * 3600,
    )
    entry.pop("setup_error", None)
    return token


def _finish_setup(entry: dict[str, Any]) -> None:
    for key in ("setup", "setup_sha256", "setup_expires_at", "setup_error"):
        entry.pop(key, None)


def setup_link(settings: Settings, token: str) -> str:
    return f"{settings.public_url}{_SETUP_PATH}?token={token}"


def send_setup_email(settings: Settings, to: str, user_id: str, link: str, *, reset: bool) -> None:
    """Send the setup link through the admin's mail server (MCP_SMTP_*)."""
    hours = f"{settings.setup_link_hours:g}"
    if reset:
        subject = "Reset your Anki MCP password"
        body = (
            f"Someone asked to reset the password for the Anki MCP account '{user_id}' at "
            f"{settings.public_url}.\n\nChoose a new password here:\n{link}\n\n"
            f"The link works once and expires in {hours} hours. If you did not ask for this, "
            "ignore this email; your password stays the same.\n"
        )
    else:
        subject = "You're invited to Anki MCP"
        body = (
            f"You've been invited to the Anki MCP server at {settings.public_url}, as user "
            f"'{user_id}'. It lets AI assistants work with your Anki collection.\n\n"
            f"Set up your account here:\n{link}\n\n"
            f"You'll choose a password, then sign in to AnkiWeb so your collection can be "
            f"copied over. The link expires in {hours} hours.\n"
        )
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    if settings.smtp_security == "tls":
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=30, context=context
        )
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    with client:
        if settings.smtp_security == "starttls":
            client.starttls(context=context)
        if settings.smtp_username:
            password = ""
            if settings.smtp_password_file is not None:
                password = settings.smtp_password_file.read_text(encoding="utf-8").strip()
            client.login(settings.smtp_username, password)
        client.send_message(message)


@dataclass
class _Job:
    task: asyncio.Task[None] | None = None
    service: AnkiCollectionService | None = None
    phase: str = "collection"
    media: dict[str, Any] | None = None


class _SetupFailed(Exception):
    pass


def _failure_message(exc: BaseException) -> str:
    if isinstance(exc, _SetupFailed):
        return str(exc)
    if isinstance(exc, SyncError) and exc.kind == SyncErrorKind.AUTH:
        return "The sync server no longer accepts your login. Sign in again from a new invite."
    if isinstance(exc, NetworkError | SyncError | TimeoutError):
        return "The sync server could not be reached. Try again in a few minutes."
    return "Something went wrong while copying your collection. Try again, or ask your admin."


class Registration:
    """The /setup pages and the first-sync jobs they start."""

    def __init__(self, settings: Settings, users: UserStore, registry: CollectionRegistry) -> None:
        self._settings = settings
        self._users = users
        self._registry = registry
        self._jobs: dict[str, _Job] = {}

    def routes(self) -> list[Route]:
        return [Route(_SETUP_PATH, self.handle, methods=["GET", "POST"])]

    async def close(self) -> None:
        for job in list(self._jobs.values()):
            if job.task is not None:
                job.task.cancel()

    def _user_for(self, token: str) -> User | None:
        if not token:
            return None
        supplied = _token_hash(token)
        match = None
        for user in self._users.all():
            # Compare against every user so timing does not reveal which entry matched.
            if user.setup_sha256 is not None and hmac.compare_digest(supplied, user.setup_sha256):
                match = user
        if match is None or match.setup is None:
            return None
        if match.setup in _EXPIRING_STAGES and match.setup_expires_at <= time.time():
            return None
        return match

    async def _update(self, user_id: str, change: Any) -> Any:
        def apply(raw: dict[str, Any]) -> Any:
            entry = raw["users"].get(user_id)
            if entry is None:
                raise LookupError(user_id)
            return change(entry)

        return await asyncio.to_thread(update_users_file, self._settings.users_file, apply)

    async def handle(self, request: Request) -> Response:
        form = await request.form() if request.method == "POST" else None
        token = str(form.get("token", "")) if form is not None else ""
        token = token or request.query_params.get("token", "")
        user = self._user_for(token)
        if user is None:
            return page(
                "Link expired",
                "<h1>This link no longer works</h1><p>It has expired or has already been used. "
                "Ask your admin for a new one.</p>",
                400,
            )
        if user.setup in {"password", "reset"}:
            if form is not None:
                return await self._set_password(user, token, form)
            return self._password_page(user, token)
        if user.setup == "ankiweb":
            if form is not None:
                return await self._sign_in_to_ankiweb(user, token, form)
            return self._ankiweb_page(user, token)
        if user.setup == "failed" and form is not None:
            await self._update(user.id, lambda entry: entry.update(setup="syncing"))
            self._start_job(user.id)
            return RedirectResponse(f"{_SETUP_PATH}?token={token}", status_code=303)
        if user.setup == "failed":
            return self._failed_page(user, token)
        if user.setup == "syncing":
            self._start_job(user.id)
            return await self._progress_page(user)
        await self._update(user.id, _finish_setup)
        return self._done_page(user)

    def _password_page(
        self, user: User, token: str, message: str = "", status: int = 200
    ) -> Response:
        reset = user.setup == "reset"
        error = f"<p class='alert' role='alert'>{html.escape(message)}</p>" if message else ""
        heading = "Choose a new password" if reset else "Welcome to Anki MCP"
        intro = (
            ""
            if reset
            else "<p>Step 1 of 3. Choose the password you'll use to connect AI assistants to "
            "your Anki collection.</p>"
        )
        return page(
            heading,
            f"<h1>{heading}</h1>{intro}{error}<form method='post'>"
            f"<input type='hidden' name='token' value='{html.escape(token)}'>"
            f"<label>User <input value='{html.escape(user.id)}' readonly "
            f"autocomplete='username'></label>"
            f"<label>New password <input type='password' name='password' required "
            f"minlength='{_MIN_PASSWORD}' autocomplete='new-password' autofocus></label>"
            f"<label>Repeat password <input type='password' name='repeat' required "
            f"minlength='{_MIN_PASSWORD}' autocomplete='new-password'></label>"
            f"<button type='submit'>{'Change password' if reset else 'Continue'}</button>"
            f"</form><p class='muted after'>At least {_MIN_PASSWORD} characters.</p>",
            status,
        )

    async def _set_password(self, user: User, token: str, form: Any) -> Response:
        from anki_mcp.oauth import hash_password

        password = str(form.get("password", ""))
        if len(password) < _MIN_PASSWORD:
            return self._password_page(
                user, token, f"Use at least {_MIN_PASSWORD} characters.", 400
            )
        if password != str(form.get("repeat", "")):
            return self._password_page(user, token, "The passwords don't match.", 400)
        password_hash = await asyncio.to_thread(hash_password, password)
        if user.setup == "reset":

            def reset(entry: dict[str, Any]) -> None:
                entry["password_hash"] = password_hash
                _finish_setup(entry)

            await self._update(user.id, reset)
            return page(
                "Password changed",
                "<h1>Password changed</h1><p class='ok'>Your new password is set. Sign in "
                "with it the next time your app asks.</p>",
            )

        def next_step(entry: dict[str, Any]) -> str:
            entry["password_hash"] = password_hash
            # A fresh link for the next step, so the emailed one stops working.
            return begin_setup(entry, "ankiweb", self._settings.setup_link_hours)

        next_token = await self._update(user.id, next_step)
        return RedirectResponse(f"{_SETUP_PATH}?token={next_token}", status_code=303)

    def _sync_server_name(self, user: User) -> str:
        if user.sync_endpoint is None:
            return "AnkiWeb"
        return html.escape(user.sync_endpoint)

    def _ankiweb_page(
        self, user: User, token: str, message: str = "", status: int = 200
    ) -> Response:
        server = self._sync_server_name(user)
        error = f"<p class='alert' role='alert'>{html.escape(message)}</p>" if message else ""
        return page(
            f"Sign in to {server}",
            f"<h1>Connect your collection</h1>"
            f"<p>Step 2 of 3. Sign in to {server} so your cards and media can be copied to "
            f"this server and kept in sync.</p>{error}<form method='post'>"
            f"<input type='hidden' name='token' value='{html.escape(token)}'>"
            f"<label>{server} email or username <input name='username' required "
            f"value='{html.escape(user.sync_username)}' autocomplete='off' "
            f"autocapitalize='none' spellcheck='false' autofocus></label>"
            f"<label>{server} password <input type='password' name='password' required "
            f"autocomplete='off'></label><button type='submit'>Sign in and copy</button></form>"
            f"<p class='muted after'>Your {server} password is used once to sign in and is "
            f"not stored; this server keeps only the login key {server} returns.</p>",
            status,
        )

    async def _sign_in_to_ankiweb(self, user: User, token: str, form: Any) -> Response:
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        if not username or not password:
            return self._ankiweb_page(user, token, "Enter your email and password.", 400)
        try:
            async with self._registry.lease(user.id) as service:
                await service.sync_login(username, password, user.sync_endpoint)
        except SyncError as exc:
            if exc.kind != SyncErrorKind.AUTH:
                return self._ankiweb_page(user, token, _failure_message(exc), 502)
            return self._ankiweb_page(
                user, token, f"{self._sync_server_name(user)} didn't accept that login.", 401
            )
        except (NetworkError, TimeoutError) as exc:
            return self._ankiweb_page(user, token, _failure_message(exc), 502)

        def syncing(entry: dict[str, Any]) -> None:
            entry.update(sync_username=username, setup="syncing")

        await self._update(user.id, syncing)
        self._start_job(user.id)
        return RedirectResponse(f"{_SETUP_PATH}?token={token}", status_code=303)

    def _start_job(self, user_id: str) -> None:
        if user_id in self._jobs:
            return
        job = _Job()
        self._jobs[user_id] = job
        job.task = asyncio.create_task(self._run_job(user_id, job))

    async def _run_job(self, user_id: str, job: _Job) -> None:
        try:
            async with self._registry.lease(user_id) as service:
                job.service = service
                user = self._users.get(user_id)
                if user is None:
                    return
                await service.bootstrap(
                    "download_if_empty", user.sync_username, None, user.sync_endpoint
                )
                job.phase = "media"
                while True:
                    status = await service.status()
                    media: dict[str, Any] = status["media_sync"]
                    job.media = media
                    if status["last_media_sync_at"]:
                        break
                    if media["state"] == "failed":
                        code = media.get("error") or "unknown"
                        raise _SetupFailed(
                            f"Your media could not be downloaded ({code}). Try again."
                        )
                    if not media["in_progress"]:
                        await service.sync(True)
                        continue
                    await asyncio.sleep(_POLL_SECONDS)
            await self._update(user_id, lambda entry: entry.update(setup="done"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "first sync failed user=%s exception_type=%s", user_id, type(exc).__name__
            )
            message = _failure_message(exc)
            await self._update(
                user_id, lambda entry: entry.update(setup="failed", setup_error=message)
            )
        finally:
            self._jobs.pop(user_id, None)

    async def _progress_page(self, user: User) -> Response:
        job = self._jobs.get(user.id)
        server = self._sync_server_name(user)
        if job is not None and job.phase == "media":
            media = job.media or {}
            counts = " · ".join(
                html.escape(str(media[key])) for key in ("checked", "added") if media.get(key)
            )
            detail = (
                "<p>Step 3 of 3. Your cards are here; now downloading media (images, audio and "
                "fonts). Anki doesn't know the total until it finishes.</p>"
                "<progress aria-label='Downloading media'></progress>"
                f"<p class='muted'>{counts or 'Checking which files are needed…'}</p>"
            )
        else:
            transferred, total = (
                await job.service.full_sync_progress()
                if job is not None and job.service is not None
                else (0, 0)
            )
            if total:
                percent = min(100, transferred * 100 // total)
                detail = (
                    "<p>Step 3 of 3. Downloading your cards.</p>"
                    f"<progress value='{transferred}' max='{total}' "
                    f"aria-label='Downloading cards'>{percent}%</progress>"
                    f"<p class='muted'>{percent}% · {transferred / 1e6:.1f} of "
                    f"{total / 1e6:.1f} MB</p>"
                )
            else:
                detail = (
                    f"<p>Step 3 of 3. Connecting to {server}…</p>"
                    "<progress aria-label='Connecting'></progress>"
                )
        return page(
            "Copying your collection",
            f"<h1>Copying your collection</h1>{detail}"
            "<p class='muted after'>This page updates itself. You can close it and come back "
            "with the same link; the copy keeps going. You can connect your apps once it has "
            "finished.</p>",
            refresh_seconds=_REFRESH_SECONDS,
        )

    def _failed_page(self, user: User, token: str) -> Response:
        message = html.escape(user.setup_error or _failure_message(Exception()))
        return page(
            "Copy failed",
            f"<h1>The copy didn't finish</h1><p class='alert' role='alert'>{message}</p>"
            f"<form method='post'><input type='hidden' name='token' value='{html.escape(token)}'>"
            "<button type='submit'>Try again</button></form>"
            "<p class='muted after'>Anything already downloaded is kept.</p>",
        )

    def _done_page(self, user: User) -> Response:
        url = html.escape(f"{self._settings.public_url}{self._settings.mcp_path}")
        return page(
            "You're all set",
            "<h1>You're all set</h1><p class='ok'>Your collection and media are on the "
            "server.</p><p>In your AI app, add a remote MCP server with this URL, then sign in "
            f"as <strong>{html.escape(user.id)}</strong> with the password you chose:</p>"
            f"<p><code>{url}</code></p>",
        )
