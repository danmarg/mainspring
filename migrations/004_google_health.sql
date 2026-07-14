-- Google Health API OAuth token storage (replaces Fitbit Web API, sunset Sep 2026)
-- Same single-row pattern as fitbit_oauth. Google refresh tokens don't expire
-- unless revoked, so expires_at tracks the access token expiry only.
CREATE TABLE IF NOT EXISTS google_health_oauth (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    TEXT NOT NULL,   -- UTC ISO-8601, access token only
    updated_at    TEXT NOT NULL
);

-- Activities from Google Health API (parallel to fitbit_activities / garmin_activities)
CREATE TABLE IF NOT EXISTS google_health_activities (
    activity_id  TEXT PRIMARY KEY,   -- Google Health session ID
    date         TEXT NOT NULL,
    start_time   TEXT,               -- UTC ISO-8601
    type         TEXT,               -- normalized type (running, cycling, etc.)
    duration_s   INTEGER,
    distance_m   REAL,
    avg_hr       REAL,
    calories     REAL,
    raw_json     TEXT,
    fetched_at   TEXT NOT NULL
);
