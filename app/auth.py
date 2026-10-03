"""
Single-password auth shared by every protected surface (admin/import, export,
Datasette, dashboard, MCP OAuth login).

MAINSPRING_PASSWORD is the one secret. For a painless rollout the surface's
legacy variable (ADMIN_TOKEN / EXPORT_TOKEN / DATASETTE_TOKEN / MCP_TOKEN) is
still accepted while it is set — unset those Fly secrets to finish collapsing to
one password. All comparisons are constant-time, and failed attempts are rate
limited per client IP.
"""

import hashlib
import hmac
import logging
import os
import threading
import time

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

PASSWORD_ENV = "MAINSPRING_PASSWORD"

# legacy per-surface variable names, accepted alongside the password while set
ADMIN = "ADMIN_TOKEN"
EXPORT = "EXPORT_TOKEN"
DATASETTE = "DATASETTE_TOKEN"
MCP = "MCP_TOKEN"


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def accepted_secrets(legacy_env: str | None = None) -> list[str]:
    secrets = [_env(PASSWORD_ENV)]
    if legacy_env:
        secrets.append(_env(legacy_env))
    return [s for s in secrets if s]


def configured(legacy_env: str | None = None) -> bool:
    return bool(accepted_secrets(legacy_env))


def verify(candidate: str | None, legacy_env: str | None = None) -> bool:
    """Constant-time check of candidate against every accepted secret."""
    if not candidate:
        return False
    cand = candidate.strip().encode()
    ok = False
    for secret in accepted_secrets(legacy_env):
        ok |= hmac.compare_digest(cand, secret.encode())
    return ok


def session_value(secret: str, purpose: str) -> str:
    """Cookie value derived from a secret — HMAC'd per purpose so a cookie for one
    surface is useless on another and never contains the secret itself."""
    return hmac.new(secret.encode(), f"mainspring:{purpose}".encode(), hashlib.sha256).hexdigest()


def verify_session(value: str | None, purpose: str, legacy_env: str | None = None) -> bool:
    if not value:
        return False
    ok = False
    try:
        candidate = value.encode()  # bytes: compare_digest raises on non-ASCII str
    except Exception:
        return False
    for secret in accepted_secrets(legacy_env):
        ok |= hmac.compare_digest(candidate, session_value(secret, purpose).encode())
    return ok


def primary_session_value(purpose: str, legacy_env: str | None = None) -> str | None:
    secrets = accepted_secrets(legacy_env)
    return session_value(secrets[0], purpose) if secrets else None


# ── rate limiting ─────────────────────────────────────────────────────────────

class RateLimiter:
    """In-memory failed-attempt limiter (single process, so no shared store)."""

    def __init__(self, max_failures: int = 10, window_s: int = 600):
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window_s]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) >= self.max_failures

    def fail(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            if len(self._failures) > 10_000:  # bound memory: drop expired buckets
                for k in list(self._failures):
                    self._recent(k, now)
            self._recent(key, now)
            self._failures.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


limiter = RateLimiter()


def client_ip(headers, peer: str | None) -> str:
    """Caller IP for rate limiting. Fly-Client-IP is only trusted on Fly itself
    (where the proxy sets it); elsewhere it's attacker-controlled, so use the peer."""
    if os.getenv("FLY_APP_NAME"):
        forwarded = headers.get("fly-client-ip")
        if forwarded:
            return forwarded
    return peer or "unknown"


def client_key(request: Request) -> str:
    return client_ip(request.headers, request.client.host if request.client else None)


def require_bearer(legacy_env: str):
    """FastAPI dependency factory: Authorization: Bearer <password>."""
    def dependency(request: Request):
        if not configured(legacy_env):
            raise HTTPException(status_code=503, detail="auth not configured")
        key = client_key(request)
        if limiter.blocked(key):
            raise HTTPException(status_code=429, detail="too many failed attempts")
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if not verify(token, legacy_env):
            limiter.fail(key)
            raise HTTPException(status_code=401, detail="invalid token")
    return dependency
