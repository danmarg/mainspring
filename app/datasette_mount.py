"""
Datasette sub-application, mounted at /datasette by main.py.

Two hard requirements (plan §6):
  - Open the DB read-only (files= mode) so no Datasette query path can write.
  - Gate with auth — the entire health history is behind this.

Auth flow:
  1. Bearer token header  (curl)
  2. Basic auth header    (browser login dialog — triggers cookie)
  3. ?token= query param  (bookmarkable URL — triggers cookie)
  4. Session cookie       (set after any of the above succeed; survives redirects)

Browsers drop Authorization headers on redirects (Datasette redirects internally),
so we set a short-lived HttpOnly cookie on first successful auth and check it on
every subsequent request.
"""

import base64
import hashlib
import os
from urllib.parse import parse_qs

from app.db import DB_PATH

_COOKIE_NAME = "ms_ds_auth"
_COOKIE_MAX_AGE = 8 * 3600  # 8 hours


class _TokenMiddleware:
    def __init__(self, app, token: str):
        self._app = app
        self._token = token
        # Store a hash of the token as the cookie value — no raw secret in cookies.
        self._cookie_val = hashlib.sha256(token.encode()).hexdigest()

    def _check_auth(self, scope) -> tuple[bool, bool]:
        """Return (authenticated, already_has_session_cookie)."""
        headers = {k.lower(): v for k, v in scope.get("headers", [])}

        # Session cookie (fast path — survives redirects)
        cookie_str = headers.get(b"cookie", b"").decode()
        for part in cookie_str.split(";"):
            name, _, val = part.strip().partition("=")
            if name.strip() == _COOKIE_NAME and val.strip() == self._cookie_val:
                return True, True

        auth = headers.get(b"authorization", b"").decode()

        # Bearer token
        if auth.lower().startswith("bearer ") and auth[7:].strip() == self._token:
            return True, False

        # Basic auth
        if auth.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth[6:].strip()).decode()
                password = decoded.split(":", 1)[-1].strip()
                if password == self._token:
                    return True, False
            except Exception:
                pass

        # Query param
        qs = parse_qs(scope.get("query_string", b"").decode())
        if qs.get("token", [None])[0] == self._token:
            return True, False

        return False, False

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        authenticated, has_cookie = self._check_auth(scope)

        if not authenticated:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Basic realm="mainspring-datasette"'),
                ],
            })
            await send({"type": "http.response.body", "body": b'{"detail":"unauthorized"}'})
            return

        # Starlette strips the mount prefix (/datasette) from scope["path"] but NOT from
        # scope["raw_path"]. Datasette 0.65 prefers raw_path when present, which causes
        # it to see the full /datasette/health/activities path and then prepend base_url
        # again, doubling the prefix in generated links. Strip raw_path to match path.
        raw_path = scope.get("raw_path")
        if raw_path:
            # raw_path includes the query string; preserve it after stripping the prefix
            prefix = b"/datasette"
            if raw_path.startswith(prefix):
                raw_path = raw_path[len(prefix):] or b"/"
        scope = {**scope, "root_path": "", "raw_path": raw_path}

        cookie_header = (
            f"{_COOKIE_NAME}={self._cookie_val}; "
            f"Path=/datasette; HttpOnly; SameSite=Lax; Max-Age={_COOKIE_MAX_AGE}; Secure"
        ).encode()

        async def _patched_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))

                # Datasette redirects its ASGI root ("/") back to root_path ("/datasette")
                # instead of to the database page, creating an infinite loop with Starlette's
                # trailing-slash 307. Rewrite that specific redirect to the health database.
                new_headers = []
                for k, v in headers:
                    if k.lower() == b"location" and v.rstrip(b"/") == b"/datasette":
                        v = b"/datasette/health"
                    new_headers.append((k, v))

                if not has_cookie:
                    new_headers.append((b"set-cookie", cookie_header))

                message = {**message, "headers": new_headers}
            await send(message)

        await self._app(scope, receive, _patched_send)


def build_datasette_app():
    """
    Return an ASGI app for mounting at /datasette, or None if DATASETTE_TOKEN
    is not set (caller skips the mount).
    """
    token = (os.getenv("DATASETTE_TOKEN") or "").strip()
    if not token:
        return None

    from datasette.app import Datasette

    db_path = str(DB_PATH)
    ds = Datasette(
        files=[db_path],
        settings={
            "base_url": "/datasette/",
            "sql_time_limit_ms": 5000,
            "max_returned_rows": 10000,
            "default_page_size": 100,
        },
        metadata={
            "title": "mainspring",
            "description": "Personal health data",
            "databases": {
                "health": {"description": "Health metrics and logs"},
            },
        },
    )

    return _TokenMiddleware(ds.app(), token)
