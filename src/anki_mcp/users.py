"""Multi-user accounts: the users file, its admin command, and the per-user collection registry.

Multi-user mode is on when MCP_USERS_FILE is set. Each user gets their own collection under
<ANKI_COLLECTION_PATH's directory>/users/<id>/, opened on first use and closed when idle.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import hmac
import logging
import re
import secrets
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


def valid_user_id(user_id: str) -> bool:
    return _USER_ID.fullmatch(user_id) is not None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class User:
    id: str
    password_hash: str | None
    token_sha256: str | None
    sync_username: str
    sync_password: str | None
    sync_endpoint: str | None


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
            token_sha256=entry.get("token_sha256") or None,
            sync_username=str(entry.get("sync_username") or ""),
            sync_password=entry.get("sync_password") or None,
            sync_endpoint=endpoint or None,
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

    def by_token(self, token: str) -> User | None:
        self._reload()
        supplied = token_hash(token)
        match = None
        for user in self._users.values():
            # Compare against every user so timing does not reveal which entry matched.
            if user.token_sha256 is not None and hmac.compare_digest(supplied, user.token_sha256):
                match = user
        return match


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


def _load_raw(path: Path) -> dict[str, Any]:
    raw = PersistentState._load_json(path) if path.exists() else None
    return raw or {"users": {}}


def _save_raw(path: Path, raw: dict[str, Any]) -> None:
    _parse_users(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    PersistentState._atomic_json(path, raw, secret=True)


def _prompt_password(prompt: str, minimum: int) -> str:
    password = getpass.getpass(f"{prompt}: ")
    if password != getpass.getpass(f"Repeat {prompt.lower()}: "):
        sys.exit("Passwords do not match.")
    if len(password) < minimum:
        sys.exit(f"Use at least {minimum} characters.")
    return password


def main(argv: list[str] | None = None) -> None:
    from anki_mcp.oauth import hash_password

    parser = argparse.ArgumentParser(prog="anki-mcp-user", description=__doc__)
    parser.add_argument("--file", type=Path, required=True, help="path of MCP_USERS_FILE")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="create a user; prints their bearer token once")
    add.add_argument("user_id")
    add.add_argument("--sync-username", default="")
    add.add_argument("--sync-host", default="", help="empty for AnkiWeb")
    add.add_argument("--sync-password", action="store_true", help="prompt for the sync password")
    add.add_argument("--no-login-password", action="store_true", help="bearer token only")
    commands.add_parser("list", help="list user ids")
    for name, text in (
        ("remove", "delete a user's account; their data directory is kept"),
        ("passwd", "set a user's OAuth login password"),
        ("token", "replace a user's bearer token; prints it once"),
    ):
        commands.add_parser(name, help=text).add_argument("user_id")
    args = parser.parse_args(argv)

    raw = _load_raw(args.file)
    users: dict[str, Any] = raw["users"]
    if args.command == "list":
        for user_id in sorted(users):
            print(user_id)
        return
    if not valid_user_id(args.user_id):
        sys.exit("User ids use a-z, 0-9, _ and -, start with a letter or digit, at most 64.")
    if args.command != "add" and args.user_id not in users:
        sys.exit(f"No user {args.user_id!r}.")

    if args.command == "add":
        if args.user_id in users:
            sys.exit(f"User {args.user_id!r} already exists.")
        try:
            endpoint = validate_sync_endpoint(args.sync_host)
        except ValueError as exc:
            sys.exit(str(exc))
        token = secrets.token_urlsafe(32)
        entry: dict[str, Any] = {
            "token_sha256": token_hash(token),
            "sync_username": args.sync_username,
            "sync_endpoint": endpoint,
        }
        if not args.no_login_password:
            entry["password_hash"] = hash_password(_prompt_password("Login password", 12))
        if args.sync_password:
            entry["sync_password"] = _prompt_password("Sync password", 1)
        users[args.user_id] = entry
        _save_raw(args.file, raw)
        print(token)
    elif args.command == "remove":
        del users[args.user_id]
        _save_raw(args.file, raw)
        print(f"Removed {args.user_id}. Their collection directory was not deleted.")
    elif args.command == "passwd":
        users[args.user_id]["password_hash"] = hash_password(_prompt_password("Login password", 12))
        _save_raw(args.file, raw)
    else:
        token = secrets.token_urlsafe(32)
        users[args.user_id]["token_sha256"] = token_hash(token)
        _save_raw(args.file, raw)
        print(token)


if __name__ == "__main__":
    main()
