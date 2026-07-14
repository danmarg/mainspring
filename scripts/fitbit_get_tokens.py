#!/usr/bin/env python3
"""
One-time helper: complete Fitbit OAuth2 authorization and print tokens as JSON.

Prerequisites:
  1. Create a Fitbit app at https://dev.fitbit.com/apps/new
     - OAuth 2.0 Application Type: Personal
     - Callback URL: http://localhost:8080/callback
  2. Set FITBIT_CLIENT_ID and FITBIT_CLIENT_SECRET in your environment,
     or enter them when prompted.

Run:
  python scripts/fitbit_get_tokens.py | curl -X POST \\
    https://your-app.fly.dev/admin/fitbit/init_tokens \\
    -H "Authorization: Bearer $ADMIN_TOKEN" \\
    -H "Content-Type: application/json" -d @-
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone


REDIRECT_URI = "http://localhost:8080/callback"
AUTH_URL = "https://www.fitbit.com/oauth2/authorize"
TOKEN_URL = "https://api.fitbit.com/oauth2/token"
SCOPES = (
    "activity heartrate sleep respiratory_rate "
    "oxygen_saturation cardio_fitness profile"
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def main():
    # credentials
    client_id = os.getenv("FITBIT_CLIENT_ID") or input("Fitbit client ID: ").strip()
    client_secret = os.getenv("FITBIT_CLIENT_SECRET") or input("Fitbit client secret: ").strip()

    # PKCE
    code_verifier = _b64url(secrets.token_bytes(32))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(params)

    print(f"\nOpen this URL in your browser:\n\n  {auth_url}\n", file=sys.stderr)
    webbrowser.open(auth_url)
    print("Waiting for callback on http://localhost:8080/callback ...", file=sys.stderr)

    # local server to catch callback
    auth_code = None

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            auth_code = qs.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorized! You can close this tab.</h1>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", 8080), _Handler)
    server.handle_request()

    if not auth_code:
        print("No auth code received.", file=sys.stderr)
        sys.exit(1)

    # exchange code for tokens
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
    }).encode()

    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req) as resp:
        token_data = json.loads(resp.read())

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])
    ).isoformat()

    output = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "expires_at": expires_at,
    }
    print(json.dumps(output))
    print("\nPipe output to POST /admin/fitbit/init_tokens", file=sys.stderr)


if __name__ == "__main__":
    main()
