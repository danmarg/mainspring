-- Baseline schema — applied on every startup via executescript (all statements are idempotent).

PRAGMA journal_mode=WAL;

-- append-only landing zone: the untouched upstream response for every fetch.
-- Never updated, only inserted. All parsing is replayable from here.
CREATE TABLE IF NOT EXISTS raw_import_payloads (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source       TEXT NOT NULL,        -- 'garmin' | 'google_health' | ...
  endpoint     TEXT NOT NULL,        -- which API call produced this payload
  date         TEXT,                 -- health date this payload covers, if applicable
  payload_json TEXT NOT NULL,        -- verbatim upstream response
  fetched_at   TEXT NOT NULL         -- UTC ISO-8601
);

-- source-agnostic scalar biometrics: every daily metric from every source
CREATE TABLE IF NOT EXISTS raw_daily_metrics (
  date       TEXT NOT NULL,
  source     TEXT NOT NULL,          -- 'garmin' | 'google_health' | ...
  metric     TEXT NOT NULL,          -- 'resting_hr' | 'hrv' | 'sleep_score' | ...
  value      REAL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (date, source, metric)
);

-- per-metric canonical source preference (absent row = fall back to DEFAULT_SOURCE_PRIORITY)
CREATE TABLE IF NOT EXISTS source_config (
  metric           TEXT PRIMARY KEY,
  canonical_source TEXT NOT NULL
);

-- per-source activity tables (structured, not EAV — too many fields)
CREATE TABLE IF NOT EXISTS garmin_activities (
  activity_id               TEXT PRIMARY KEY,
  date                      TEXT,
  start_time                TEXT,
  type                      TEXT,
  duration_s                INTEGER,
  distance_m                REAL,
  avg_hr                    INTEGER,
  max_hr                    INTEGER,
  training_effect_aerobic   REAL,
  training_effect_anaerobic REAL,
  calories                  INTEGER,
  decoupling_pct            REAL,   -- aerobic decoupling: % HR:pace drift, first half vs second half
  raw_json                  TEXT,
  fetched_at                TEXT
);

CREATE TABLE IF NOT EXISTS google_health_activities (
  activity_id TEXT PRIMARY KEY,
  date        TEXT,
  start_time  TEXT,
  type        TEXT,
  duration_s  INTEGER,
  distance_m  REAL,
  avg_hr      REAL,
  calories    REAL,
  raw_json    TEXT,
  fetched_at  TEXT NOT NULL
);

-- normalized, deduped activities for analysis/Datasette
CREATE TABLE IF NOT EXISTS activities (
  id                        INTEGER PRIMARY KEY AUTOINCREMENT,
  date                      TEXT,
  start_time                TEXT,
  type                      TEXT,
  duration_s                INTEGER,
  distance_m                REAL,
  avg_hr                    INTEGER,
  max_hr                    INTEGER,
  calories                  INTEGER,
  decoupling_pct            REAL,
  canonical_source          TEXT,
  garmin_activity_id        TEXT,
  google_health_activity_id TEXT
);

-- Garmin daily workout recommendation
CREATE TABLE IF NOT EXISTS suggested_workouts (
  date                TEXT NOT NULL,
  source              TEXT NOT NULL,
  workout_type        TEXT,
  description         TEXT,
  target_duration_min REAL,
  target_intensity    TEXT,
  raw_json            TEXT,
  fetched_at          TEXT NOT NULL,
  PRIMARY KEY (date, source)
);

-- intraday HR: 2-min native Garmin intervals averaged to 1-min buckets (~720 rows/day)
CREATE TABLE IF NOT EXISTS intraday_hr (
  ts     TEXT NOT NULL,   -- UTC ISO-8601 second precision, e.g. 2026-07-12T04:00:00Z
  source TEXT NOT NULL,
  bpm    REAL NOT NULL,
  PRIMARY KEY (ts, source)
);

-- intraday stress: 3-min native Garmin intervals; -1/-2 (unmeasured/activity) excluded
CREATE TABLE IF NOT EXISTS intraday_stress (
  ts     TEXT NOT NULL,   -- UTC ISO-8601 second precision
  source TEXT NOT NULL,
  stress REAL NOT NULL,
  PRIMARY KEY (ts, source)
);

-- intraday HRV (RMSSD): Fitbit sleep-night samples via Google Health Connect (~5-min resolution)
-- HRV4Training morning readings also land here when RMSSD is available
CREATE TABLE IF NOT EXISTS intraday_hrv (
  ts     TEXT NOT NULL,   -- UTC ISO-8601 second precision
  source TEXT NOT NULL,
  rmssd  REAL NOT NULL,
  PRIMARY KEY (ts, source)
);

-- manual logs written by Claude via MCP tools
CREATE TABLE IF NOT EXISTS manual_logs (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                    TEXT NOT NULL,        -- event time, UTC ISO-8601
  type                  TEXT NOT NULL,        -- 'meal' | 'caffeine' | 'alcohol' | 'note'
  description           TEXT,
  quantity              REAL,
  unit                  TEXT,
  estimated_calories    INTEGER,
  estimated_macros_json TEXT,                 -- {"protein_g":…, "carbs_g":…, "fat_g":…}
  confidence            TEXT,                 -- 'photo_estimate' | 'user_confirmed' | ...
  created_at            TEXT NOT NULL,        -- UTC ISO-8601, when logged (vs. when it happened)
  garmin_synced_at      TEXT                  -- UTC ISO-8601 when pushed (or marked not-applicable); NULL = pending push
);

-- per-day timezone, derived from device data (GPS / local offset in raw payloads)
CREATE TABLE IF NOT EXISTS day_timezone (
  date   TEXT PRIMARY KEY,
  tz     TEXT NOT NULL,    -- IANA zone or fixed offset
  source TEXT NOT NULL     -- 'garmin_activity' | 'garmin_sleep' | 'home_default' | ...
);

-- normalized wide view rebuilt by normalization job after each import
CREATE TABLE IF NOT EXISTS daily_metrics (
  date                 TEXT PRIMARY KEY,
  resting_hr           REAL,
  hrv                  REAL,
  sleep_score          REAL,
  sleep_duration_min   REAL,
  sleep_deep_min       REAL,
  sleep_rem_min        REAL,
  sleep_light_min      REAL,
  body_battery_high    REAL,
  body_battery_low     REAL,
  stress_avg           REAL,
  training_readiness   REAL,
  vo2max               REAL,
  steps                REAL,
  active_zone_minutes  REAL,
  spo2_avg             REAL,
  breathing_rate       REAL,
  caffeine_mg          REAL,
  alcohol_units        REAL,
  calories_estimated   REAL,
  source_flags_json    TEXT,   -- {"metric": "source_used", ...}
  weight_kg            REAL,
  acute_training_load  REAL,
  chronic_training_load REAL,
  training_load_ratio  REAL,
  bp_systolic          REAL,
  bp_diastolic         REAL,
  bp_pulse             REAL,
  rpe                  REAL,
  skin_temp_deviation  REAL,  -- deg C vs personal baseline, illness/cycle-shift signal
  hydration_ml         REAL,
  max_hr               REAL,  -- observed max HR for the day; feeds hr_zones
  lactate_threshold_hr REAL,
  lactate_threshold_pace_min_per_km REAL,
  ftp_watts            REAL,  -- cycling functional threshold power
  sleep_breathing_rate REAL,  -- overnight-only respiration, distinct from all-day breathing_rate
  recovery_hours       REAL   -- estimated hours until ready for hard training again
);

-- personalized HR zone boundaries, derived from max_hr (%HRmax bands) — source-agnostic,
-- not scraped from any single vendor's proprietary zone endpoint
CREATE TABLE IF NOT EXISTS hr_zones (
  date    TEXT NOT NULL,
  source  TEXT NOT NULL,   -- 'derived' (computed from max_hr) | vendor name if ever sourced directly
  zone    INTEGER NOT NULL,-- 1-5
  min_bpm REAL,
  max_bpm REAL,
  PRIMARY KEY (date, source, zone)
);

-- Google Health API OAuth token storage (single row, id=1 enforced by CHECK)
CREATE TABLE IF NOT EXISTS google_health_oauth (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  access_token  TEXT NOT NULL,
  refresh_token TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- audit log for import runs
CREATE TABLE IF NOT EXISTS import_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source        TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT,
  status        TEXT,
  rows_upserted INTEGER,
  error         TEXT
);

-- MCP OAuth 2.1 tables (single-user)
CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
  client_id   TEXT PRIMARY KEY,
  client_json TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_pending_auth (
  session_id  TEXT PRIMARY KEY,
  client_id   TEXT NOT NULL,
  params_json TEXT NOT NULL,
  expires_at  REAL NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_auth_codes (
  code       TEXT PRIMARY KEY,
  code_json  TEXT NOT NULL,
  expires_at REAL NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_access_tokens (
  token      TEXT PRIMARY KEY,
  token_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_refresh_tokens (
  token      TEXT PRIMARY KEY,
  token_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- steady-state weekly training targets
CREATE TABLE IF NOT EXISTS training_goals (
  metric     TEXT PRIMARY KEY,
  value      REAL NOT NULL,
  unit       TEXT,
  updated_at TEXT NOT NULL
);

-- tracks which dates have had the morning webhook sent (prevents duplicate fires)
CREATE TABLE IF NOT EXISTS morning_webhooks (
  date    TEXT PRIMARY KEY,
  sent_at TEXT NOT NULL
);

-- goal races / training events
CREATE TABLE IF NOT EXISTS training_events (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  date             TEXT NOT NULL,
  type             TEXT NOT NULL,
  description      TEXT NOT NULL,
  goal_description TEXT,
  status           TEXT NOT NULL DEFAULT 'upcoming',
  result           TEXT,
  created_at       TEXT NOT NULL
);
