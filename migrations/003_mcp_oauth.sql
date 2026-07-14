-- OAuth 2.1 tables for MCP server (single-user, personal service)
CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
    client_id   TEXT PRIMARY KEY,
    client_json TEXT NOT NULL,   -- serialized OAuthClientInformationFull
    created_at  TEXT NOT NULL
);

-- Pending auth sessions: hold OAuth params while user enters PIN
CREATE TABLE IF NOT EXISTS mcp_pending_auth (
    session_id  TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    params_json TEXT NOT NULL,   -- serialized AuthorizationParams
    expires_at  REAL NOT NULL,   -- unix timestamp
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_auth_codes (
    code       TEXT PRIMARY KEY,
    code_json  TEXT NOT NULL,    -- serialized AuthorizationCode
    expires_at REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_access_tokens (
    token      TEXT PRIMARY KEY,
    token_json TEXT NOT NULL,    -- serialized AccessToken
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_refresh_tokens (
    token      TEXT PRIMARY KEY,
    token_json TEXT NOT NULL,    -- serialized RefreshToken
    created_at TEXT NOT NULL
);
