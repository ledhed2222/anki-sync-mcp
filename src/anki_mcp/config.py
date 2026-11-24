from __future__ import annotations

from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_sync_endpoint(value: str) -> str:
    """Validate a custom sync endpoint without exposing embedded credentials."""
    if any(character.isspace() for character in value):
        raise ValueError("sync endpoint must not contain whitespace")
    endpoint = value.strip()
    if not endpoint:
        return ""
    parsed = urlsplit(endpoint)
    if len(endpoint.encode("utf-8")) > 2048:
        raise ValueError("sync endpoint must not exceed 2048 UTF-8 bytes")
    try:
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("sync endpoint contains an invalid hostname") from exc
    if not parsed.netloc or not hostname or parsed.scheme not in {"http", "https"}:
        raise ValueError("sync endpoint must be empty or an HTTP(S) base URL")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("sync endpoint contains an invalid port") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("sync endpoint must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError("sync endpoint must not contain a query or fragment")
    loopback_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and parsed.hostname not in loopback_hosts:
        raise ValueError("sync endpoint requires HTTPS except for loopback development")
    return endpoint


def validate_sync_migration_endpoint(value: str, configured_endpoint: str | None) -> str:
    """Restrict server-directed migrations to an explicitly trusted origin."""
    endpoint = validate_sync_endpoint(value)
    parsed = urlsplit(endpoint)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if configured_endpoint:
        configured = urlsplit(configured_endpoint)
        configured_hostname = (configured.hostname or "").casefold().rstrip(".")
        configured_port = configured.port or (443 if configured.scheme == "https" else 80)
        if (parsed.scheme, hostname, port) != (
            configured.scheme,
            configured_hostname,
            configured_port,
        ):
            raise ValueError("sync endpoint migration left the configured trusted origin")
    elif parsed.scheme != "https" or not (
        hostname == "ankiweb.net" or hostname.endswith(".ankiweb.net")
    ):
        raise ValueError("AnkiWeb endpoint migration targeted an untrusted origin")
    return endpoint


# Public static-content hosts: npm/GitHub CDNs (Fontsource fonts), Google Fonts files and
# Wikipedia/Wikimedia Commons media.
_DEFAULT_MEDIA_URL_HOSTS = (
    "cdn.jsdelivr.net",
    "fonts.gstatic.com",
    "raw.githubusercontent.com",
    "unpkg.com",
    "upload.wikimedia.org",
)


def _parse_media_url_hosts(entries: list[str], source: str) -> frozenset[str]:
    hosts = set()
    for entry in entries:
        host = entry.split("#", 1)[0].strip().casefold().rstrip(".")
        if not host:
            continue
        if any(character in host for character in "/:@") or any(c.isspace() for c in host):
            raise ValueError(f"{source} entries must be bare hostnames, got {entry.strip()!r}")
        hosts.add(host)
    return frozenset(hosts)


class Settings(BaseSettings):
    """Validated process configuration."""

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=True,
        extra="forbid",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    host: str = Field("0.0.0.0", alias="MCP_HOST")
    port: int = Field(8000, ge=1, le=65535, alias="MCP_PORT")
    mcp_path: str = Field("/mcp", alias="MCP_PATH")
    public_url: str = Field(alias="MCP_PUBLIC_URL")
    oauth_redirect_uris_csv: str = Field(
        "https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback",
        alias="MCP_OAUTH_REDIRECT_URIS",
    )
    data_dir: Path = Field(Path("/data"), alias="ANKI_DATA_DIR")
    max_open_collections: int = Field(100, ge=1, le=10_000, alias="ANKI_MAX_OPEN_COLLECTIONS")
    collection_idle_seconds: float = Field(
        900, ge=10, le=86_400, alias="ANKI_COLLECTION_IDLE_SECONDS"
    )
    media_root: Path | None = Field(None, alias="ANKI_MEDIA_ROOT")
    backup_root: Path | None = Field(None, alias="ANKI_BACKUP_ROOT")
    scopes_csv: str = Field("read,write,admin", alias="MCP_SCOPES")
    sync_on_read: bool = Field(False, alias="ANKI_SYNC_ON_READ")
    sync_on_write: bool = Field(True, alias="ANKI_SYNC_ON_WRITE")
    allow_destructive: bool = Field(False, alias="ANKI_ALLOW_DESTRUCTIVE")
    allow_full_sync: bool = Field(False, alias="ANKI_ALLOW_FULL_SYNC")
    allow_schema_changes: bool = Field(False, alias="ANKI_ALLOW_SCHEMA_CHANGES")
    allow_review_answers: bool = Field(False, alias="ANKI_ALLOW_REVIEW_ANSWERS")
    confirmation_ttl_seconds: int = Field(
        300, ge=30, le=3600, alias="ANKI_CONFIRMATION_TTL_SECONDS"
    )
    confirmation_mode: Literal["token", "elicit", "elicit_required"] = Field(
        "token", alias="ANKI_CONFIRMATION_MODE"
    )
    bootstrap_mode: Literal["disabled", "download_if_empty"] = Field(
        "disabled", alias="ANKI_BOOTSTRAP_MODE"
    )
    max_batch_size: int = Field(50, ge=1, le=500, alias="ANKI_MAX_BATCH_SIZE")
    max_page_size: int = Field(100, ge=1, le=1000, alias="MCP_MAX_PAGE_SIZE")
    max_search_scan: int = Field(10_000, ge=1, le=1_000_000, alias="MCP_MAX_SEARCH_SCAN")
    max_rendered_field_bytes: int = Field(
        262_144, ge=1, le=4_194_304, alias="MCP_MAX_RENDERED_FIELD_BYTES"
    )
    max_card_fields: int = Field(100, ge=1, le=1000, alias="MCP_MAX_CARD_FIELDS")
    max_media_bytes: int = Field(1_048_576, ge=1, le=16_777_216, alias="ANKI_MAX_MEDIA_BYTES")
    sync_timeout_seconds: float = Field(300, gt=0, le=3600, alias="ANKI_SYNC_TIMEOUT_SECONDS")
    media_sync_wait_seconds: float = Field(10, ge=0, le=60, alias="ANKI_MEDIA_SYNC_WAIT_SECONDS")
    initial_media_sync: bool = Field(True, alias="ANKI_INITIAL_MEDIA_SYNC")
    media_url_hosts_csv: str | None = Field(None, alias="ANKI_MEDIA_URL_HOSTS")
    media_url_hosts_file: Path | None = Field(None, alias="ANKI_MEDIA_URL_HOSTS_FILE")
    media_url_hosts: frozenset[str] = Field(default=frozenset(), exclude=True)
    max_response_bytes: int = Field(
        1_048_576, ge=1024, le=16_777_216, alias="MCP_MAX_RESPONSE_BYTES"
    )
    max_request_bytes: int = Field(1_048_576, ge=1024, le=16_777_216, alias="MCP_MAX_REQUEST_BYTES")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    allowed_hosts_csv: str = Field(
        "testserver,localhost,localhost:*,127.0.0.1,127.0.0.1:*,anki-mcp,anki-mcp:*",
        alias="MCP_ALLOWED_HOSTS",
    )
    allowed_origins_csv: str = Field(
        "http://localhost:*,http://127.0.0.1:*", alias="MCP_ALLOWED_ORIGINS"
    )

    @property
    def users_file(self) -> Path:
        return self.data_dir / "users.json"

    def user_collection_path(self, user_id: str) -> Path:
        return self.data_dir / "users" / user_id / "collection.anki2"

    @field_validator("mcp_path")
    @classmethod
    def valid_mcp_path(cls, value: str) -> str:
        if not value.startswith("/") or value == "/":
            raise ValueError("MCP_PATH must be an absolute, non-root path")
        if any(character in value for character in "{}?#") or any(
            character.isspace() for character in value
        ):
            raise ValueError("MCP_PATH must be a static URL path without templates or whitespace")
        value = value.rstrip("/")
        if value == "/health" or value.startswith("/health/"):
            raise ValueError("MCP_PATH must not overlap the reserved /health routes")
        return value

    @property
    def oauth_redirect_uris(self) -> frozenset[str]:
        return frozenset(
            item.strip() for item in self.oauth_redirect_uris_csv.split(",") if item.strip()
        )

    @model_validator(mode="after")
    def validate_public_url(self) -> Self:
        parsed = urlsplit(self.public_url)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.netloc or not (parsed.scheme == "https" or loopback):
            raise ValueError("MCP_PUBLIC_URL must be HTTPS except for loopback development")
        if parsed.query or parsed.fragment or parsed.path.strip("/"):
            raise ValueError("MCP_PUBLIC_URL must be an origin without path, query or fragment")
        self.public_url = f"{parsed.scheme}://{parsed.netloc}"
        if not self.oauth_redirect_uris:
            raise ValueError("MCP_OAUTH_REDIRECT_URIS must list at least one redirect URI")
        return self

    @property
    def allowed_hosts(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts_csv.split(",") if item.strip()]

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins_csv.split(",") if item.strip()]

    @model_validator(mode="after")
    def resolve_media_url_hosts(self) -> Self:
        if self.media_url_hosts_csv is not None and self.media_url_hosts_file is not None:
            raise ValueError(
                "set at most one of ANKI_MEDIA_URL_HOSTS and ANKI_MEDIA_URL_HOSTS_FILE"
            )
        if self.media_url_hosts_file is not None:
            try:
                lines = self.media_url_hosts_file.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise ValueError("unable to read ANKI_MEDIA_URL_HOSTS_FILE") from exc
            self.media_url_hosts = _parse_media_url_hosts(lines, "ANKI_MEDIA_URL_HOSTS_FILE")
        elif self.media_url_hosts_csv is not None:
            self.media_url_hosts = _parse_media_url_hosts(
                self.media_url_hosts_csv.split(","), "ANKI_MEDIA_URL_HOSTS"
            )
        else:
            self.media_url_hosts = frozenset(_DEFAULT_MEDIA_URL_HOSTS)
        return self

    @property
    def scopes(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.scopes_csv.split(",") if item.strip())

    @field_validator("scopes_csv")
    @classmethod
    def valid_scopes(cls, value: str) -> str:
        scopes = {item.strip() for item in value.split(",") if item.strip()}
        unknown = scopes - {"read", "write", "admin", "destructive"}
        if unknown:
            raise ValueError(f"unknown MCP scope: {', '.join(sorted(unknown))}")
        return value

    @model_validator(mode="after")
    def validate_response_budget(self) -> Self:
        minimum = self.max_rendered_field_bytes + 4096
        if self.max_response_bytes < minimum:
            raise ValueError(
                "MCP_MAX_RESPONSE_BYTES must be at least MCP_MAX_RENDERED_FIELD_BYTES + 4096"
            )
        return self
