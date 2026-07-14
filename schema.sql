-- v1 baseline schema — run once on fresh DB
-- All subsequent changes go in migrations/NNN_xxx.sql

PRAGMA journal_mode=WAL;

-- append-only landing zone: the untouched upstream response for every fetch.
-- Never updated, only inserted. All parsing is replayable from here.
CREATE TABLE IF NOT EXISTS raw_import_payloads (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,        -- 'garmin' | 'google_health' | ...
  endpoint    TEXT NOT NULL,        -- which API call produced this payload
  date        TEXT,                 -- health date this payload covers, if applicable
  payload_json TEXT NOT NULL,       -- verbatim upstream response
  fetched_at  TEXT NOT NULL         -- UTC ISO-8601
);

-- source-agnostic scalar biometrics: every daily metric from every source
CREATE TABLE IF NOT EXISTS raw_daily_metrics (
  date       TEXT NOT NULL,
  source     TEXT NOT NULL,         -- 'garmin' | 'google_health' | ...
  metric     TEXT NOT NULL,         -- 'resting_hr' | 'hrv' | 'sleep_score' | ...
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
  activity_id                 TEXT PRIMARY KEY,
  date                        TEXT,
  start_time                  TEXT,
  type                        TEXT,
  duration_s                  INTEGER,
  distance_m                  REAL,
  avg_hr                      INTEGER,
  max_hr                      INTEGER,
  training_effect_aerobic     REAL,
  training_effect_anaerobic   REAL,
  calories                    INTEGER,
  raw_json                    TEXT,
  fetched_at                  TEXT
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
  id                         INTEGER PRIMARY KEY AUTOINCREMENT,
  date                       TEXT,
  start_time                 TEXT,
  type                       TEXT,
  duration_s                 INTEGER,
  distance_m                 REAL,
  avg_hr                     INTEGER,
  max_hr                     INTEGER,
  calories                   INTEGER,
  canonical_source           TEXT,
  garmin_activity_id         TEXT,
  google_health_activity_id  TEXT
);

-- Garmin daily workout recommendation
CREATE TABLE IF NOT EXISTS suggested_workouts (
  date                 TEXT NOT NULL,
  source               TEXT NOT NULL,
  workout_type         TEXT,
  description          TEXT,
  target_duration_min  REAL,
  target_intensity     TEXT,
  raw_json             TEXT,
  fetched_at           TEXT NOT NULL,
  PRIMARY KEY (date, source)
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
  created_at            TEXT NOT NULL         -- UTC ISO-8601, when logged (vs. when it happened)
);

-- per-day timezone, derived from device data (GPS / local offset in raw payloads)
CREATE TABLE IF NOT EXISTS day_timezone (
  date   TEXT PRIMARY KEY,
  tz     TEXT NOT NULL,    -- IANA zone or fixed offset
  source TEXT NOT NULL     -- 'garmin_activity' | 'garmin_sleep' | 'home_default' | ...
);

-- normalized wide view rebuilt by normalization job after each import
CREATE TABLE IF NOT EXISTS daily_metrics (
  date                TEXT PRIMARY KEY,
  resting_hr          REAL,
  hrv                 REAL,
  sleep_score         REAL,
  sleep_duration_min  REAL,
  sleep_deep_min      REAL,
  sleep_rem_min       REAL,
  sleep_light_min     REAL,
  body_battery_high   REAL,
  body_battery_low    REAL,
  stress_avg          REAL,
  training_readiness  REAL,
  vo2max              REAL,
  steps               REAL,
  active_zone_minutes REAL,
  spo2_avg            REAL,
  breathing_rate      REAL,
  caffeine_mg         REAL,
  alcohol_units       REAL,
  calories_estimated  REAL,
  source_flags_json   TEXT   -- {"metric": "source_used", ...}
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
