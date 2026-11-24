"""One-time move of a pre-multi-user install into users/owner/.

Older versions kept one collection at ANKI_COLLECTION_PATH (default /data/collection.anki2),
with state/ and backups/ beside it, and took the owner's credentials from environment
variables. This moves those files into the owner's directory, writes the owner into the users
file, and keeps already-issued OAuth tokens working by assigning them to the owner.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from anki_mcp.config import Settings, validate_sync_endpoint
from anki_mcp.state import PersistentState
from anki_mcp.users import OWNER, load_users_file, save_users_file

_LOGGER = logging.getLogger(__name__)
_IGNORED_SETTINGS = (
    "MCP_AUTH_MODE",
    "MCP_AUTH_TOKEN",
    "MCP_AUTH_TOKEN_FILE",
    "MCP_OAUTH_PASSWORD_HASH",
    "MCP_OAUTH_PASSWORD_HASH_FILE",
    "MCP_USERS_FILE",
    "ANKI_COLLECTION_PATH",
    "ANKI_SYNC_USERNAME",
    "ANKI_SYNC_PASSWORD",
    "ANKI_SYNC_PASSWORD_FILE",
    "ANKI_SYNC_HOST",
)


def _legacy_secret(environ: Mapping[str, str], name: str) -> str | None:
    direct = environ.get(name)
    if direct:
        return direct
    path = environ.get(f"{name}_FILE")
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8").removesuffix("\n") or None
    except OSError:
        _LOGGER.warning("could not read %s_FILE during migration; skipping it", name)
        return None


def _owner_entry(environ: Mapping[str, str]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "sync_username": environ.get("ANKI_SYNC_USERNAME", ""),
        "sync_endpoint": validate_sync_endpoint(environ.get("ANKI_SYNC_HOST", "")),
    }
    password_hash = _legacy_secret(environ, "MCP_OAUTH_PASSWORD_HASH")
    if password_hash:
        entry["password_hash"] = password_hash
    sync_password = _legacy_secret(environ, "ANKI_SYNC_PASSWORD")
    if sync_password:
        entry["sync_password"] = sync_password
    return entry


def _assign_tokens_to_owner(oauth_file: Path) -> None:
    stored = PersistentState._load_json(oauth_file)
    if stored is None:
        return
    for kind in ("access", "refresh"):
        for record in stored.get(kind, {}).values():
            record["subject"] = record.get("subject") or OWNER
    PersistentState._atomic_json(oauth_file, stored, secret=True)


def migrate_legacy_install(settings: Settings, environ: Mapping[str, str] = os.environ) -> None:
    """Move an old single-collection layout into users/owner/; no-op on a current layout."""
    ignored = [name for name in _IGNORED_SETTINGS if environ.get(name)]
    if ignored:
        _LOGGER.warning(
            "ignoring settings no longer used (accounts live in %s): %s",
            settings.users_file,
            ", ".join(ignored),
        )
    data_dir = settings.data_dir
    legacy = Path(environ.get("ANKI_COLLECTION_PATH") or data_dir / "collection.anki2")
    staging = data_dir / "users" / ".owner-migrating"
    if staging.exists():
        raise RuntimeError(
            f"an earlier migration stopped part way; finish moving the files in {staging} to "
            f"{data_dir / 'users' / OWNER} by hand, then remove {staging}"
        )
    if not legacy.exists():
        if not settings.users_file.exists():
            save_users_file(settings.users_file, {"users": {}})
        return
    owner_dir = settings.user_collection_path(OWNER).parent
    if owner_dir.exists():
        raise RuntimeError(
            f"both an old collection at {legacy} and {owner_dir} exist; remove one of them"
        )

    _LOGGER.warning("moving the collection at %s to %s", legacy, owner_dir)
    staging.mkdir(parents=True)
    old_dir = legacy.parent
    # The media index is derived from the collection name; everything else lives beside it.
    moves = [
        (legacy.with_suffix(".media"), "collection.media"),
        (legacy.with_suffix(".media.db2"), "collection.media.db2"),
        (old_dir / "state", "state"),
        (old_dir / "backups", "backups"),
        (Path(f"{legacy}-wal"), "collection.anki2-wal"),
        (Path(f"{legacy}-shm"), "collection.anki2-shm"),
        (legacy, "collection.anki2"),
    ]
    for source, name in moves:
        if source.exists() or source.is_symlink():
            source.rename(staging / name)
    oauth_file = staging / "state" / "oauth.json"
    if oauth_file.exists():
        oauth_file.rename(data_dir / "oauth.json")
        _assign_tokens_to_owner(data_dir / "oauth.json")
    staging.rename(owner_dir)

    raw = load_users_file(settings.users_file)
    raw["users"].setdefault(OWNER, _owner_entry(environ))
    save_users_file(settings.users_file, raw)
    if not raw["users"][OWNER].get("password_hash"):
        _LOGGER.warning(
            "user %r has no login password yet; set one with: anki-mcp-user --file %s passwd %s",
            OWNER,
            settings.users_file,
            OWNER,
        )
