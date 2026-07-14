import logging
import os
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import init_db
from app.admin_routes import router as admin_router
from app.mcp_oauth import router as mcp_auth_router

log = logging.getLogger(__name__)

# Build MCP app now (sets mcp._session_manager as a side effect)
from app.mcp_server import build_mcp_app, mcp as _mcp_instance
_mcp_app = build_mcp_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if _mcp_app is not None:
        # StreamableHTTPSessionManager requires its task group to be started
        # via run() before any requests arrive. Starlette does NOT call mounted
        # sub-apps' lifespans automatically, so we do it here.
        async with _mcp_instance.session_manager.run():
            yield
    else:
        yield


app = FastAPI(title="mainspring", lifespan=lifespan)


@app.middleware("http")
async def normalize_mcp_path(request: Request, call_next: Callable):
    # Starlette mounts don't match the exact mount point without a trailing slash
    # (e.g. /mcp → 307 → /mcp/), and claude.ai's MCP client doesn't follow POST redirects.
    # Rewrite /mcp to /mcp/ at the ASGI scope level so the sub-app is reached directly.
    if request.scope["path"] == "/mcp":
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"
    return await call_next(request)


app.include_router(admin_router)
app.include_router(mcp_auth_router)


# Mount MCP server if MCP_TOKEN is configured (_mcp_app built at top of file)
if _mcp_app is not None:
    app.mount("/mcp", _mcp_app)
    log.info("MCP server mounted at /mcp")
else:
    log.warning("MCP_TOKEN not set — /mcp not mounted")

# Mount Datasette if DATASETTE_TOKEN is configured
from app.datasette_mount import build_datasette_app
_ds_app = build_datasette_app()
if _ds_app is not None:
    app.mount("/datasette", _ds_app)
    log.info("Datasette mounted at /datasette")
else:
    log.warning("DATASETTE_TOKEN not set — /datasette not mounted")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/.well-known/oauth-protected-resource/mcp")
async def oauth_protected_resource_metadata():
    """RFC 9396 resource metadata — tells clients where to find our OAuth server."""
    base = os.getenv("APP_BASE_URL", "https://your-app.fly.dev")
    return JSONResponse(
        {
            "resource": f"{base}/mcp",
            "authorization_servers": [f"{base}/mcp"],
            "bearer_methods_supported": ["header"],
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _auth_server_metadata(base: str) -> dict:
    return {
        "issuer": f"{base}/mcp",
        "authorization_endpoint": f"{base}/mcp/authorize",
        "token_endpoint": f"{base}/mcp/token",
        "registration_endpoint": f"{base}/mcp/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
    }


@app.get("/.well-known/oauth-authorization-server/mcp")
async def oauth_authorization_server_metadata():
    """RFC 8414 path-suffix form — tried by clients when issuer URL has a path component."""
    base = os.getenv("APP_BASE_URL", "https://your-app.fly.dev")
    return JSONResponse(_auth_server_metadata(base), headers={"Cache-Control": "public, max-age=3600"})


@app.get("/.well-known/openid-configuration/mcp")
async def openid_configuration():
    """OIDC discovery fallback — some clients try this before OAuth metadata."""
    base = os.getenv("APP_BASE_URL", "https://your-app.fly.dev")
    return JSONResponse(_auth_server_metadata(base), headers={"Cache-Control": "public, max-age=3600"})
