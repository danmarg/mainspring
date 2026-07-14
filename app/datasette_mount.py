"""
Datasette sub-application, mounted at /datasette by main.py.

Two hard requirements (plan §6):
  - Open the DB immutable so no Datasette query path can write.
  - Gate with bearer auth — the entire health history is behind this.

Uses Datasette's base_url setting so its generated links stay consistent
after FastAPI strips the /datasette prefix before forwarding requests.
"""

import os

from app.db import DB_PATH


class _BearerTokenMiddleware:
    def __init__(self, app, token: str):
        self._app = app
        self._token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode()
            if not (auth.lower().startswith("bearer ") and auth[7:] == self._token):
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="mainspring-datasette"'),
                    ],
                })
                await send({"type": "http.response.body", "body": b'{"detail":"unauthorized"}'})
                return
        await self._app(scope, receive, send)


def build_datasette_app():
    """
    Return an ASGI app for mounting at /datasette, or None if DATASETTE_TOKEN
    is not set (caller skips the mount).
    """
    token = os.getenv("DATASETTE_TOKEN")
    if not token:
        return None

    from datasette.app import Datasette

    db_path = str(DB_PATH)
    ds = Datasette(
        immutables=[db_path],
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

    return _BearerTokenMiddleware(ds.app(), token)
