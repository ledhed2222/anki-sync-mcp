"""Accounts: the users file, its admin command, and the per-user collection registry.

Every user has their own collection at <ANKI_DATA_DIR>/users/<id>/collection.anki2, opened on
first use and closed when idle. A personal install is simply one with a single user.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import getpass
import logging
import os
import re
import smtplib
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anki_mcp.collection import AnkiCollectionService, ResourceLimitError
from anki_mcp.config import validate_sync_endpoint
from anki_mcp.state import PersistentState

_LOGGER = logging.getLogger(__name__)
_USER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
OWNER = "owner"


def valid_user_id(user_id: str) -> bool:
    return _USER_ID.fullmatch(user_id) is not None


# Setup stages that keep a user from signing in: they have not finished registering.
SETUP_BLOCKS_SIGN_IN = frozenset({"password", "ankiweb", "syncing", "failed"})


@dataclass(frozen=True)
class User:
    id: str
    password_hash: str | None
    sync_username: str
    sync_password: str | None
    sync_endpoint: str | None
    email: str | None = None
    setup: str | None = None
    setup_sha256: str | None = None
    setup_expires_at: float = 0.0
    setup_error: str | None = None

    @property
    def can_sign_in(self) -> bool:
        return self.password_hash is not None and self.setup not in SETUP_BLOCKS_SIGN_IN


def _parse_users(raw: dict[str, Any]) -> dict[str, User]:
    entries = raw.get("users")
    if not isinstance(entries, dict):
        raise ValueError("users file must contain a 'users' object")
    users: dict[str, User] = {}
    for user_id, entry in entries.items():
        if not valid_user_id(user_id):
            raise ValueError(f"invalid user id {user_id!r}: use a-z, 0-9, _ and -, at most 64")
        if not isinstance(entry, dict):
            raise ValueError(f"user {user_id!r} must be an object")
        endpoint = validate_sync_endpoint(str(entry.get("sync_endpoint") or ""))
        users[user_id] = User(
            id=user_id,
            password_hash=entry.get("password_hash") or None,
            sync_username=str(entry.get("sync_username") or ""),
            sync_password=entry.get("sync_password") or None,
            sync_endpoint=endpoint or None,
            email=entry.get("email") or None,
            setup=entry.get("setup") or None,
            setup_sha256=entry.get("setup_sha256") or None,
            setup_expires_at=float(entry.get("setup_expires_at") or 0),
            setup_error=entry.get("setup_error") or None,
        )
    return users


class UserStore:
    """Read the users file, reloading it when it changes so removals take effect at once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime_ns: int | None = None
        self._users: dict[str, User] = {}
        self._reload()

    def _reload(self) -> None:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except FileNotFoundError as exc:
            raise RuntimeError(f"users file {self.path} does not exist") from exc
        if mtime_ns == self._mtime_ns:
            return
        self._users = _parse_users(PersistentState._load_json(self.path) or {"users": {}})
        self._mtime_ns = mtime_ns

    def get(self, user_id: str) -> User | None:
        self._reload()
        return self._users.get(user_id)

    def all(self) -> list[User]:
        self._reload()
        return list(self._users.values())


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    service: AnkiCollectionService | None = None
    in_use: int = 0
    last_used: float = 0.0


class CollectionRegistry:
    """Open each user's collection on first use, close idle ones, and cap how many are open.

    A user's collection is only ever opened or closed while holding that user's lock, so two
    handles on one collection file can never coexist.
    """

    def __init__(
        self,
        open_service: Callable[[str], Awaitable[AnkiCollectionService]],
        max_open: int,
        idle_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._open_service = open_service
        self._max_open = max_open
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._open_count = 0
        self.evictions = 0

    @property
    def open_count(self) -> int:
        return self._open_count

    @property
    def max_open(self) -> int:
        return self._max_open

    async def for_each_open(
        self, function: Callable[[AnkiCollectionService], Awaitable[object]]
    ) -> None:
        """Run background work on each open collection without keeping it from going idle."""
        for entry in list(self._entries.values()):
            service = entry.service
            if service is None:
                continue
            entry.in_use += 1
            try:
                await function(service)
            except Exception:
                _LOGGER.exception("background collection task failed")
            finally:
                entry.in_use -= 1

    @asynccontextmanager
    async def lease(self, user_id: str) -> AsyncIterator[AnkiCollectionService]:
        entry = self._entries.setdefault(user_id, _Entry())
        async with entry.lock:
            if entry.service is None:
                await self._make_room()
                self._open_count += 1
                try:
                    entry.service = await self._open_service(user_id)
                except BaseException:
                    self._open_count -= 1
                    raise
            entry.in_use += 1
        try:
            yield entry.service
        finally:
            entry.in_use -= 1
            entry.last_used = self._clock()

    async def _closable(self, entry: _Entry) -> bool:
        if entry.service is None or entry.in_use or entry.lock.locked():
            return False
        return not await entry.service.media_sync_active()

    async def _close(self, entry: _Entry) -> bool:
        # Never wait on another user's lock: a caller may already hold its own, so waiting here
        # could deadlock two opens against each other.
        if entry.lock.locked():
            return False
        async with entry.lock:
            if entry.service is None or entry.in_use:
                return False
            service, entry.service = entry.service, None
            self._open_count -= 1
            await service.__aexit__()
            return True

    async def _make_room(self) -> None:
        while self._open_count >= self._max_open:
            idle = sorted(
                (e for e in self._entries.values() if e.service is not None and not e.in_use),
                key=lambda e: e.last_used,
            )
            for entry in idle:
                if await self._closable(entry) and await self._close(entry):
                    self.evictions += 1
                    break
            else:
                raise ResourceLimitError(
                    "too many collections are open; retry shortly (ANKI_MAX_OPEN_COLLECTIONS)"
                )

    async def close_idle(self) -> None:
        cutoff = self._clock() - self._idle_seconds
        for entry in list(self._entries.values()):
            if (
                entry.last_used <= cutoff
                and await self._closable(entry)
                and await self._close(entry)
            ):
                self.evictions += 1

    async def close_all(self) -> None:
        for entry in self._entries.values():
            async with entry.lock:
                if entry.service is not None:
                    service, entry.service = entry.service, None
                    self._open_count -= 1
                    await service.__aexit__()


def load_users_file(path: Path) -> dict[str, Any]:
    raw = PersistentState._load_json(path) if path.exists() else None
    return raw or {"users": {}}


def save_users_file(path: Path, raw: dict[str, Any]) -> None:
    _parse_users(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    PersistentState._atomic_json(path, raw, secret=True)


def update_users_file[T](path: Path, change: Callable[[dict[str, Any]], T]) -> T:
    """Read, change and write the users file under a lock shared by the server and the CLI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = load_users_file(path)
        result = change(raw)
        save_users_file(path, raw)
        return result


def _prompt_password(prompt: str, minimum: int) -> str:
    password = getpass.getpass(f"{prompt}: ")
    if password != getpass.getpass(f"Repeat {prompt.lower()}: "):
        sys.exit("Passwords do not match.")
    if len(password) < minimum:
        sys.exit(f"Use at least {minimum} characters.")
    return password


def _password_hash(args: argparse.Namespace) -> str:
    from anki_mcp.oauth import hash_password

    if args.hash is None:
        return hash_password(_prompt_password("Login password", 12))
    if args.hash.count("$") != 5 or not args.hash.startswith("scrypt$"):
        sys.exit("--hash must be a line printed by anki-mcp-hash-password.")
    return args.hash


def _send_link(user_id: str, email: str, token: str, *, reset: bool) -> None:
    from anki_mcp.config import Settings
    from anki_mcp.registration import send_setup_email, setup_link

    settings = Settings()  # pyright: ignore[reportCallIssue] - values come from environment
    link = setup_link(settings, token)
    if not settings.smtp_host:
        print(f"MCP_SMTP_HOST is not set, so no email was sent. Send {email} this link:\n{link}")
        return
    try:
        send_setup_email(settings, email, user_id, link, reset=reset)
    except (OSError, smtplib.SMTPException) as exc:
        sys.exit(f"The link was saved but the email failed ({type(exc).__name__}); retry later.")
    print(f"Sent a {'password reset' if reset else 'setup'} link to {email}.")


def _setup_hours() -> float:
    from anki_mcp.config import Settings

    return Settings().setup_link_hours  # pyright: ignore[reportCallIssue]


def main(argv: list[str] | None = None) -> None:
    from anki_mcp.registration import begin_setup, valid_email

    parser = argparse.ArgumentParser(prog="anki-mcp-user", description=__doc__)
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(os.environ.get("ANKI_DATA_DIR", "/data")) / "users.json",
        help="users file (default: $ANKI_DATA_DIR/users.json)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list user ids")
    remove = commands.add_parser("remove", help="delete an account; its data directory is kept")
    remove.add_argument("user_id")
    hash_help = "use a hash from anki-mcp-hash-password instead of prompting"
    add = commands.add_parser("add", help="create a user with a password you type")
    add.add_argument("user_id")
    add.add_argument("--hash", help=hash_help)
    add.add_argument("--sync-username", default="")
    add.add_argument("--sync-host", default="", help="empty for AnkiWeb")
    add.add_argument("--sync-password", action="store_true", help="prompt for the sync password")
    invite = commands.add_parser("invite", help="create a user and email them a setup link")
    invite.add_argument("user_id")
    invite.add_argument("--email", help="required unless --resend finds one on file")
    invite.add_argument("--sync-host", default="", help="empty for AnkiWeb")
    invite.add_argument("--resend", action="store_true", help="new link for an unfinished setup")
    passwd = commands.add_parser("passwd", help="set a user's login password")
    passwd.add_argument("user_id")
    passwd.add_argument("--hash", help=hash_help)
    reset = commands.add_parser("reset", help="email a user a link to choose a new password")
    reset.add_argument("user_id")
    args = parser.parse_args(argv)

    if args.command == "list":
        for user_id in sorted(load_users_file(args.file)["users"]):
            print(user_id)
        return
    if not valid_user_id(args.user_id):
        sys.exit("User ids use a-z, 0-9, _ and -, start with a letter or digit, at most 64.")
    creating = args.command == "add" or (args.command == "invite" and not args.resend)
    if args.command in {"add", "invite"}:
        try:
            endpoint = validate_sync_endpoint(args.sync_host)
        except ValueError as exc:
            sys.exit(str(exc))
    if args.command == "invite" and args.email is not None and not valid_email(args.email):
        sys.exit("That doesn't look like an email address.")
    # Prompt and hash before taking the lock, so a slow typist never blocks the server.
    password_hash = _password_hash(args) if args.command in {"add", "passwd"} else None
    sync_password = (
        _prompt_password("Sync password", 1)
        if args.command == "add" and args.sync_password
        else None
    )
    hours = _setup_hours() if args.command in {"invite", "reset"} else 0.0

    def change(raw: dict[str, Any]) -> tuple[str, str] | None:
        users: dict[str, Any] = raw["users"]
        if creating and args.user_id in users:
            sys.exit(f"User {args.user_id!r} already exists.")
        if not creating and args.user_id not in users:
            sys.exit(f"No user {args.user_id!r}.")
        if args.command == "add":
            users[args.user_id] = {
                "password_hash": password_hash,
                "sync_username": args.sync_username,
                "sync_endpoint": endpoint,
            }
            if sync_password is not None:
                users[args.user_id]["sync_password"] = sync_password
        elif args.command == "remove":
            del users[args.user_id]
        elif args.command == "passwd":
            users[args.user_id]["password_hash"] = password_hash
        elif args.command == "invite" and creating:
            if args.email is None:
                sys.exit("--email is required.")
            users[args.user_id] = {
                "email": args.email,
                "sync_username": "",
                "sync_endpoint": endpoint,
            }
            return args.email, begin_setup(users[args.user_id], "password", hours)
        elif args.command == "invite":
            entry = users[args.user_id]
            if entry.get("setup") not in SETUP_BLOCKS_SIGN_IN:
                sys.exit(f"{args.user_id} has finished setup; use 'reset' to change a password.")
            entry["email"] = args.email or entry.get("email")
            if not entry["email"]:
                sys.exit("--email is required.")
            return entry["email"], begin_setup(entry, entry["setup"], hours)
        else:
            entry = users[args.user_id]
            if entry.get("setup") in SETUP_BLOCKS_SIGN_IN:
                sys.exit(f"{args.user_id} hasn't finished setup; use 'invite --resend'.")
            if not entry.get("email"):
                sys.exit(f"No email on file for {args.user_id}.")
            return entry["email"], begin_setup(entry, "reset", hours)
        return None

    sent = update_users_file(args.file, change)
    if args.command == "remove":
        print(f"Removed {args.user_id}. Their collection directory was not deleted.")
    if sent is not None:
        _send_link(args.user_id, sent[0], sent[1], reset=args.command == "reset")


if __name__ == "__main__":
    main()
