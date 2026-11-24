from __future__ import annotations

import io
import urllib.error
import urllib.request
from typing import Any

import pytest

from anki_mcp import media_url
from anki_mcp.media_url import (
    MediaDownloadError,
    check_media_filename,
    check_media_url,
    download_media,
)

HOSTS = frozenset(
    {"cdn.jsdelivr.net", "unpkg.com", "raw.githubusercontent.com", "upload.wikimedia.org"}
)
COMMIT = "0123456789abcdef0123456789abcdef01234567"
PINNED = "https://cdn.jsdelivr.net/npm/x@1.0.0/a"


class _Opener:
    def __init__(self, result: Any) -> None:
        self.result = result

    def open(self, url: str, timeout: float) -> Any:
        if isinstance(self.result, Exception):
            raise self.result
        if isinstance(self.result, io.BytesIO):
            return self.result
        return io.BytesIO(self.result)


def _serve(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    monkeypatch.setattr(media_url.urllib.request, "build_opener", lambda *_: _Opener(result))


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://cdn.jsdelivr.net/npm/x@1.0.0/a", "INVALID_ARGUMENT"),
        ("https://user:pw@cdn.jsdelivr.net/a.woff2", "INVALID_ARGUMENT"),
        ("not a url", "INVALID_ARGUMENT"),
        ("https://[cdn.jsdelivr.net]/a", "INVALID_ARGUMENT"),
        ("https://cdn.jsdelivr.net:22/a", "INVALID_ARGUMENT"),
        ("https://cdn.jsdelivr.net:99999/a", "INVALID_ARGUMENT"),
        ("https://169.254.169.254/latest", "MEDIA_URL_HOST_NOT_ALLOWED"),
        ("https://cdn.jsdelivr.net.evil.test/a", "MEDIA_URL_HOST_NOT_ALLOWED"),
    ],
)
def test_rejects_urls_off_the_allow_list(url: str, code: str) -> None:
    with pytest.raises(MediaDownloadError) as caught:
        check_media_url(url, HOSTS)
    assert caught.value.code == code


def test_accepts_listed_host_case_insensitively() -> None:
    check_media_url("https://CDN.jsdelivr.net./npm/x@1.0.0/x.woff2", HOSTS)
    check_media_url("https://cdn.jsdelivr.net:443/npm/x@1.0.0/x.woff2", HOSTS)


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.jsdelivr.net/npm/@fontsource/inter@5.0.8/files/inter-latin-400-normal.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.11-rc.1/dist/fonts/a.woff2",
        f"https://cdn.jsdelivr.net/gh/owner/repo@{COMMIT}/a.png",
        "https://unpkg.com/@fontsource/inter@5.0.8/files/a.woff2",
        f"https://raw.githubusercontent.com/owner/repo/{COMMIT}/a.png",
        "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency.png",
    ],
)
def test_pinned_urls_are_accepted(url: str) -> None:
    check_media_url(url, HOSTS)


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.jsdelivr.net/npm/@fontsource/inter@latest/files/a.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/inter@5/files/a.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/inter@^5.0.8/files/a.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/inter/files/a.woff2",
        "https://cdn.jsdelivr.net/gh/owner/repo@v1.0.0/a.png",
        "https://cdn.jsdelivr.net/combine/npm/x@1.0.0",
        "https://unpkg.com/inter/a.woff2",
        "https://raw.githubusercontent.com/owner/repo/main/a.png",
        f"https://raw.githubusercontent.com/owner/repo/{COMMIT[:7]}/a.png",
    ],
)
def test_unpinned_urls_are_refused(url: str) -> None:
    with pytest.raises(MediaDownloadError) as caught:
        check_media_url(url, HOSTS)
    assert caught.value.code == "MEDIA_URL_NOT_PINNED"


@pytest.mark.parametrize("filename", ["a.woff2", "A.PNG", "x.svg", "clip.mp3", "v.webm"])
def test_media_extensions_are_allowed(filename: str) -> None:
    check_media_filename(filename)


@pytest.mark.parametrize("filename", ["a.html", "a.js", "a.css", "noextension", "a.png.exe"])
def test_other_extensions_are_refused(filename: str) -> None:
    with pytest.raises(MediaDownloadError) as caught:
        check_media_filename(filename)
    assert caught.value.code == "MEDIA_TYPE_NOT_ALLOWED"


def test_download_stops_at_the_overall_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Drip(io.BytesIO):
        def read1(self, size: int = -1) -> bytes:
            return super().read1(1)

    clock = iter(range(0, 1000, 20))
    monkeypatch.setattr(media_url.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(media_url.urllib.request, "build_opener", lambda *_: _Opener(_Drip(b"abc")))
    with pytest.raises(MediaDownloadError, match="longer than"):
        download_media(PINNED, HOSTS, 5)


def test_redirect_off_the_allow_list_is_refused() -> None:
    handler = media_url._AllowListRedirectHandler(HOSTS)
    request = urllib.request.Request("https://cdn.jsdelivr.net/a")
    with pytest.raises(MediaDownloadError) as caught:
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.test/a")
    assert caught.value.code == "MEDIA_URL_HOST_NOT_ALLOWED"
    assert handler.redirect_request(request, None, 302, "Found", {}, PINNED)


def test_download_returns_content_within_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, b"12345")
    assert download_media(PINNED, HOSTS, 5) == b"12345"


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (b"123456", "MEDIA_TOO_LARGE"),
        (urllib.error.HTTPError("u", 404, "Not Found", {}, None), "MEDIA_URL_NOT_FOUND"),  # type: ignore[arg-type]
        (urllib.error.HTTPError("u", 500, "Error", {}, None), "MEDIA_DOWNLOAD_FAILED"),  # type: ignore[arg-type]
        (urllib.error.URLError("dns"), "MEDIA_DOWNLOAD_FAILED"),
        (TimeoutError(), "MEDIA_DOWNLOAD_FAILED"),
    ],
)
def test_download_failures_have_their_own_codes(
    monkeypatch: pytest.MonkeyPatch, result: Any, code: str
) -> None:
    _serve(monkeypatch, result)
    with pytest.raises(MediaDownloadError) as caught:
        download_media(PINNED, HOSTS, 5)
    assert caught.value.code == code
