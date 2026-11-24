"""Server-rendered HTML pages (sign-in, registration) with one shared stylesheet."""

from __future__ import annotations

import base64
import hashlib

from starlette.responses import HTMLResponse

_CSS = """
:root{color-scheme:light dark;--bg:#f4f5f7;--card:#fff;--text:#1d2125;--muted:#5e6670;
--border:#d4d8dd;--accent:#2f6fd6;--accent-text:#fff;--error-bg:#fdecec;--error:#a1260d;
--ok-bg:#e8f5ec;--ok:#1d6b35}
@media (prefers-color-scheme:dark){:root{--bg:#16181b;--card:#202327;--text:#e7e9ec;
--muted:#a3abb4;--border:#3a3f45;--accent:#6b9cf0;--accent-text:#0d1117;--error-bg:#3a1f1c;
--error:#ffb4a6;--ok-bg:#1b3324;--ok:#9be0b0}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
padding:24px 16px;background:var(--bg);color:var(--text);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{width:100%;max-width:420px;background:var(--card);border:1px solid var(--border);
border-radius:12px;padding:32px 28px}
h1{font-size:1.4rem;line-height:1.3;margin:0 0 12px}
p{margin:0 0 16px}
.muted{color:var(--muted);font-size:.9rem}
.after{margin:16px 0 0}
label{display:block;font-weight:600;margin:0 0 16px}
input{display:block;width:100%;margin-top:6px;padding:10px 12px;font:inherit;color:inherit;
background:var(--card);border:1px solid var(--border);border-radius:8px}
input:focus,button:focus-visible,a:focus-visible{outline:2px solid var(--accent);
outline-offset:2px}
button,.button{display:inline-block;width:100%;padding:11px 16px;font:inherit;font-weight:600;
text-align:center;text-decoration:none;color:var(--accent-text);background:var(--accent);
border:0;border-radius:8px;cursor:pointer}
.alert{padding:10px 12px;border-radius:8px;background:var(--error-bg);color:var(--error)}
.ok{padding:10px 12px;border-radius:8px;background:var(--ok-bg);color:var(--ok)}
progress{display:block;width:100%;height:12px;margin:8px 0 6px;accent-color:var(--accent)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em}
"""
_CSS_HASH = base64.b64encode(hashlib.sha256(_CSS.encode("utf-8")).digest()).decode()


def page(
    title: str, body: str, status: int = 200, *, refresh_seconds: int | None = None
) -> HTMLResponse:
    """Render a page; `body` must already be escaped. `refresh_seconds` reloads it in place."""
    refresh = f"<meta http-equiv='refresh' content='{refresh_seconds}'>" if refresh_seconds else ""
    return HTMLResponse(
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>{refresh}"
        f"<title>{title} · Anki MCP</title><style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>",
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                f"default-src 'none'; style-src 'sha256-{_CSS_HASH}'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
        },
    )
