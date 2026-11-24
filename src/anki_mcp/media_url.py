from __future__ import annotations

import http.client
import re
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

_DOWNLOAD_TIMEOUT_SECONDS = 30.0
_SOCKET_TIMEOUT_SECONDS = 10.0
_READ_CHUNK_BYTES = 65_536
_SEMVER = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
_NPM_PACKAGE = r"(?:@[^/@]+/)?[^/@]+"
_COMMIT = r"[0-9a-f]{40}"
# Hosts whose URLs can name content that never changes: a published npm version or a git commit.
# Git tags can be moved, so they do not count. Other hosts are not checked.
_PINNED_PATHS = {
    "cdn.jsdelivr.net": re.compile(
        rf"^/(?:npm/{_NPM_PACKAGE}@{_SEMVER}|gh/[^/@]+/[^/@]+@{_COMMIT})/"
    ),
    "unpkg.com": re.compile(rf"^/{_NPM_PACKAGE}@{_SEMVER}/"),
    "raw.githubusercontent.com": re.compile(rf"^/[^/]+/[^/]+/{_COMMIT}/"),
}
URL_MEDIA_EXTENSIONS = frozenset(
    {
        *("avif", "bmp", "gif", "ico", "jpeg", "jpg", "png", "svg", "tif", "tiff", "webp"),
        *("aac", "flac", "m4a", "mp3", "oga", "ogg", "opus", "wav"),
        *("m4v", "mkv", "mov", "mp4", "ogv", "webm"),
        *("otf", "ttf", "woff", "woff2"),
    }
)
# Wikimedia refuses urllib's generic User-Agent.
_USER_AGENT = "anki-mcp (+https://github.com/ledhed2222/anki-sync-mcp)"


class MediaDownloadError(Exception):
    """A media URL download failure carrying its stable tool error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def check_media_url(url: str, hosts: frozenset[str]) -> None:
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError as exc:
        raise MediaDownloadError("INVALID_ARGUMENT", "url contains an invalid hostname") from exc
    if parsed.scheme != "https" or not hostname:
        raise MediaDownloadError("INVALID_ARGUMENT", "url must be an https URL")
    if parsed.username is not None or parsed.password is not None:
        raise MediaDownloadError("INVALID_ARGUMENT", "url must not contain user information")
    try:
        port = parsed.port
    except ValueError as exc:
        raise MediaDownloadError("INVALID_ARGUMENT", "url contains an invalid port") from exc
    if port not in {None, 443}:
        raise MediaDownloadError("INVALID_ARGUMENT", "url must use the default HTTPS port")
    if hostname not in hosts:
        raise MediaDownloadError(
            "MEDIA_URL_HOST_NOT_ALLOWED", f"host {hostname} is not listed in ANKI_MEDIA_URL_HOSTS"
        )
    pinned = _PINNED_PATHS.get(hostname)
    if pinned is not None and not pinned.match(parsed.path):
        raise MediaDownloadError(
            "MEDIA_URL_NOT_PINNED",
            f"{hostname} urls must name an exact package version (pkg@1.2.3) or a full git "
            "commit hash, not a tag, branch, range or latest",
        )


def check_media_filename(filename: str) -> None:
    extension = filename.rpartition(".")[2].casefold() if "." in filename else ""
    if extension not in URL_MEDIA_EXTENSIONS:
        raise MediaDownloadError(
            "MEDIA_TYPE_NOT_ALLOWED",
            "media stored from a url must have an image, audio, video or font extension",
        )


class _AllowListRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: frozenset[str]) -> None:
        self.hosts = hosts

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        check_media_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_media(url: str, hosts: frozenset[str], max_bytes: int) -> bytes:
    check_media_url(url, hosts)
    opener = urllib.request.build_opener(_AllowListRedirectHandler(hosts))
    opener.addheaders = [("User-Agent", _USER_AGENT)]
    # The socket timeout bounds each read; the deadline bounds a server that drips bytes.
    deadline = time.monotonic() + _DOWNLOAD_TIMEOUT_SECONDS
    content = bytearray()
    try:
        with opener.open(url, timeout=_SOCKET_TIMEOUT_SECONDS) as response:
            while chunk := response.read1(_READ_CHUNK_BYTES):
                content += chunk
                if len(content) > max_bytes:
                    raise MediaDownloadError(
                        "MEDIA_TOO_LARGE",
                        f"downloaded media exceeds ANKI_MAX_MEDIA_BYTES ({max_bytes})",
                    )
                if time.monotonic() > deadline:
                    raise MediaDownloadError(
                        "MEDIA_DOWNLOAD_FAILED",
                        f"media download took longer than {_DOWNLOAD_TIMEOUT_SECONDS:.0f}s",
                    )
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 410}:
            raise MediaDownloadError(
                "MEDIA_URL_NOT_FOUND", f"url returned HTTP {exc.code}"
            ) from exc
        raise MediaDownloadError("MEDIA_DOWNLOAD_FAILED", f"url returned HTTP {exc.code}") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise MediaDownloadError("MEDIA_DOWNLOAD_FAILED", "media download failed") from exc
    return bytes(content)
