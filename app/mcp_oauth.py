"""
OAuth 2.1 provider for the MCP server.

Single-user design: the "login" is just entering MCP_TOKEN as a PIN.
All state lives in the SQLite DB.
"""

import json
import os
import secrets
import time
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.db import get_connection, utc_now

router = APIRouter(prefix="/mcp-auth")


def _callback_page(callback_url: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorized</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 400px; margin: 80px auto; padding: 0 1rem; text-align: center; }}
    h1 {{ font-size: 1.25rem; }}
    p {{ color: #666; font-size: 0.9rem; }}
    a {{ display: inline-block; margin-top: 1rem; padding: 0.6rem 1.5rem;
         background: #1a1a1a; color: white; border-radius: 4px; text-decoration: none; font-size: 1rem; }}
    a:hover {{ background: #333; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #111; color: #eee; }}
      p {{ color: #999; }}
      a {{ background: #eee; color: #111; }}
    }}
  </style>
</head>
<body>
  <h1>Authorized ✓</h1>
  <p>Tap the button below to return to Claude and complete the connection.</p>
  <a href="{callback_url}">Return to Claude</a>
  <script>
    // attempt automatic redirect; if iOS opens the native app instead,
    // the user can tap the button above to retry in the browser
    setTimeout(function() {{ window.location.href = "{callback_url}"; }}, 500);
  </script>
</body>
</html>"""

ACCESS_TOKEN_TTL = 3600        # 1 hour
REFRESH_TOKEN_TTL = 30 * 86400 # 30 days
AUTH_CODE_TTL = 300            # 5 minutes
PENDING_AUTH_TTL = 600         # 10 minutes


def _db():
    return get_connection()


# ── OAuth provider ────────────────────────────────────────────────────────────

class MainspringOAuthProvider:
    """OAuthAuthorizationServerProvider backed by SQLite."""

    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT client_json FROM mcp_oauth_clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return OAuthClientInformationFull(**json.loads(row[0]))

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO mcp_oauth_clients(client_id, client_json, created_at) "
                "VALUES (?,?,?) "
                "ON CONFLICT(client_id) DO UPDATE SET client_json=excluded.client_json",
                (client_info.client_id, client_info.model_dump_json(), utc_now()),
            )
            conn.commit()
        finally:
            conn.close()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        session_id = secrets.token_urlsafe(32)
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO mcp_pending_auth(session_id, client_id, params_json, expires_at, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    session_id,
                    client.client_id,
                    params.model_dump_json(),
                    time.time() + PENDING_AUTH_TTL,
                    utc_now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return f"{self._base_url}/mcp-auth/login?session={session_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT code_json, expires_at FROM mcp_auth_codes WHERE code=?",
                (authorization_code,),
            ).fetchone()
        finally:
            conn.close()
        if not row or time.time() > row[1]:
            return None
        return AuthorizationCode(**json.loads(row[0]))

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = int(time.time())

        at = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
        )
        rt = RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
        )

        conn = _db()
        try:
            conn.execute(
                "INSERT INTO mcp_access_tokens(token, token_json, created_at) VALUES (?,?,?)",
                (access_token, at.model_dump_json(), utc_now()),
            )
            conn.execute(
                "INSERT INTO mcp_refresh_tokens(token, token_json, created_at) VALUES (?,?,?)",
                (refresh_token, rt.model_dump_json(), utc_now()),
            )
            conn.execute("DELETE FROM mcp_auth_codes WHERE code=?", (authorization_code.code,))
            conn.commit()
        finally:
            conn.close()

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT token_json FROM mcp_access_tokens WHERE token=?", (token,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        at = AccessToken(**json.loads(row[0]))
        if at.expires_at and int(time.time()) > at.expires_at:
            return None
        return at

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT token_json FROM mcp_refresh_tokens WHERE token=?", (refresh_token,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        rt = RefreshToken(**json.loads(row[0]))
        if rt.expires_at and int(time.time()) > rt.expires_at:
            return None
        return rt

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        access_token = secrets.token_urlsafe(32)
        now = int(time.time())
        at = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
        )
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO mcp_access_tokens(token, token_json, created_at) VALUES (?,?,?)",
                (access_token, at.model_dump_json(), utc_now()),
            )
            conn.commit()
        finally:
            conn.close()
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh_token.token,
            scope=" ".join(at.scopes),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        conn = _db()
        try:
            if isinstance(token, AccessToken):
                conn.execute("DELETE FROM mcp_access_tokens WHERE token=?", (token.token,))
            else:
                conn.execute("DELETE FROM mcp_refresh_tokens WHERE token=?", (token.token,))
            conn.commit()
        finally:
            conn.close()


# ── login page (FastAPI routes on main app) ───────────────────────────────────

_LOGIN_PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mainspring — Authorize Claude</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 400px; margin: 80px auto; padding: 0 1rem; }}
    h1 {{ font-size: 1.25rem; margin-bottom: 0.25rem; }}
    p {{ color: #666; margin-bottom: 1.5rem; font-size: 0.9rem; }}
    input {{ width: 100%; padding: 0.5rem; font-size: 1rem; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; margin-bottom: 1rem; }}
    button {{ width: 100%; padding: 0.6rem; background: #1a1a1a; color: white; border: none; border-radius: 4px; font-size: 1rem; cursor: pointer; }}
    button:hover {{ background: #333; }}
    .error {{ color: #c00; margin-bottom: 1rem; font-size: 0.9rem; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #111; color: #eee; }}
      p {{ color: #999; }}
      input {{ background: #222; color: #eee; border-color: #444; }}
      button {{ background: #eee; color: #111; }}
      button:hover {{ background: #ccc; }}
    }}
  </style>
</head>
<body>
  <h1>Authorize Claude</h1>
  <p>Enter your Mainspring access token to allow Claude to read and log your health data.</p>
  {error}
  <form method="post">
    <input type="hidden" name="session" value="{session}">
    <input type="password" name="pin" placeholder="Access token" autofocus>
    <button type="submit">Authorize</button>
  </form>
</body>
</html>"""


@router.get("/login", response_class=HTMLResponse)
async def login_page(session: str, error: str = ""):
    err_html = f'<p class="error">{error}</p>' if error else ""
    return _LOGIN_PAGE.format(session=session, error=err_html)


@router.post("/login")
async def login_submit(request: Request, session: str = Form(...), pin: str = Form(...)):
    expected = os.getenv("MCP_TOKEN")
    if not expected or pin != expected:
        return RedirectResponse(
            f"/mcp-auth/login?session={session}&error=Invalid+token",
            status_code=303,
        )

    # look up pending auth session
    conn = _db()
    try:
        row = conn.execute(
            "SELECT client_id, params_json, expires_at FROM mcp_pending_auth WHERE session_id=?",
            (session,),
        ).fetchone()
    finally:
        conn.close()

    if not row or time.time() > row[2]:
        return HTMLResponse("Session expired. Please retry the authorization.", status_code=400)

    client_id, params_json, _ = row
    params = AuthorizationParams(**json.loads(params_json))

    # generate auth code
    code = secrets.token_urlsafe(32)
    auth_code = AuthorizationCode(
        code=code,
        scopes=params.scopes or [],
        expires_at=time.time() + AUTH_CODE_TTL,
        client_id=client_id,
        code_challenge=params.code_challenge,
        redirect_uri=params.redirect_uri,
        redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
    )

    conn = _db()
    try:
        conn.execute(
            "INSERT INTO mcp_auth_codes(code, code_json, expires_at, created_at) VALUES (?,?,?,?)",
            (code, auth_code.model_dump_json(), auth_code.expires_at, utc_now()),
        )
        conn.execute("DELETE FROM mcp_pending_auth WHERE session_id=?", (session,))
        conn.commit()
    finally:
        conn.close()

    # build the callback URL
    redirect_uri = str(params.redirect_uri)
    sep = "&" if "?" in redirect_uri else "?"
    callback_url = f"{redirect_uri}{sep}code={code}"
    if params.state:
        callback_url += f"&state={params.state}"

    # Use JS redirect + manual link instead of a server 303 redirect.
    # On iOS, a server-side redirect to claude.ai triggers Universal Links
    # and opens the native app rather than completing the browser OAuth flow.
    return HTMLResponse(_callback_page(callback_url))
