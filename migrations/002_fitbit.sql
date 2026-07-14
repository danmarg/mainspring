-- Fitbit OAuth token storage (single row, id=1 enforced by CHECK)
-- Client credentials live in FITBIT_CLIENT_ID / FITBIT_CLIENT_SECRET env vars.
-- Access + refresh tokens live here so the importer can update them on refresh.
CREATE TABLE IF NOT EXISTS fitbit_oauth (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    TEXT NOT NULL,   -- UTC ISO-8601
    updated_at    TEXT NOT NULL
);

-- Fitbit-specific metrics (no Garmin equivalent)
ALTER TABLE daily_metrics ADD COLUMN readiness_score    REAL;
ALTER TABLE daily_metrics ADD COLUMN active_zone_minutes REAL;
ALTER TABLE daily_metrics ADD COLUMN spo2_avg           REAL;
ALTER TABLE daily_metrics ADD COLUMN breathing_rate     REAL;

-- Sleep stages (useful from both sources; add here rather than in baseline)
ALTER TABLE daily_metrics ADD COLUMN sleep_deep_min  REAL;
ALTER TABLE daily_metrics ADD COLUMN sleep_rem_min   REAL;
ALTER TABLE daily_metrics ADD COLUMN sleep_light_min REAL;
