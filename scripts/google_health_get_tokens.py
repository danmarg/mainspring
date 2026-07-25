#!/usr/bin/env python3
"""
Fetch initial Google Health API OAuth2 tokens.

Usage:
    python scripts/google_health_get_tokens.py \
        --client-id YOUR_CLIENT_ID \
        --client-secret YOUR_CLIENT_SECRET

Then upload the result to your app:
    curl -X POST $APP_BASE_URL/admin/google_health/init_tokens \
        -H "Authorization: Bearer $ADMIN_TOKEN" \
        -H "Content-Type: application/json" \
        -d @google_health_tokens.json
"""

import argparse
import json
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event

REDIRECT_URI = "http://localhost:8080/callback"
SCOPES = " ".join([
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
])
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _exchange_code(code: str, client_id: str, client_secret: str) -> dict:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--output", default="google_health_tokens.json")
    args = parser.parse_args()

    state = secrets.token_urlsafe(16)
    done = Event()
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query))

            if parsed.path != "/callback":
                self.send_response(404); self.end_headers(); return

            if params.get("state") != state:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"state mismatch"); return

            if "error" in params:
                self.send_response(400); self.end_headers()
                self.wfile.write(params["error"].encode()); return

            try:
                tokens = _exchange_code(params["code"], args.client_id, args.client_secret)
                result.update(tokens)
                self.send_response(200); self.end_headers()
                self.wfile.write(b"Authorized! You can close this tab.")
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(str(e).encode())
            finally:
                done.set()

    auth_params = urllib.parse.urlencode({
        "client_id": args.client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",   # force refresh_token to be returned
    })
    print(f"\nOpen this URL in your browser:\n\n{AUTH_URL}?{auth_params}\n")

    server = HTTPServer(("localhost", 8080), Handler)
    server.timeout = 1
    while not done.is_set():
        server.handle_request()
    server.server_close()

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=result.get("expires_in", 3600))
    ).isoformat()

    output = {
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
        "expires_at": expires_at,
    }
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nTokens written to {args.output}")
    print(f"\nUpload with:")
    print(f'  curl -X POST ${{APP_BASE_URL}}/admin/google_health/init_tokens \\')
    print(f'    -H "Authorization: Bearer $ADMIN_TOKEN" \\')
    print(f'    -H "Content-Type: application/json" \\')
    print(f'    -d @{args.output}')


if __name__ == "__main__":
    main()
