# Personal Health Data Service — Build Plan

## Goal

A single self-hosted service that becomes the source of truth for personal
health data: Garmin/Fitbit biometrics (HRV, sleep, stress, training
readiness, activities) plus manually logged nutrition, caffeine, and alcohol
(entered conversationally via Claude, including from photos). The point is
to enable lag-correlation analysis between behavior (diet, caffeine,
alcohol) and performance/recovery (HRV, sleep, stress, training readiness)
— not just storage for its own sake.

### What "provider-independent" does and doesn't mean here

This makes you independent of the providers' **retention and analysis** — you
own the full history in a format you control and can run correlations their
apps will never offer. It does **not** make you independent of their
**collection**: the data still originates on their devices and flows through
their cloud APIs, so Garmin and Fitbit remain upstream single points of
failure. In particular, `cyberjunky/python-garminconnect` is an unofficial,
reverse-engineered wrapper — Garmin can break auth or add MFA/Cloudflare
challenges with no notice, and Fitbit's official API is rate-limited,
OAuth-gated, and has been progressively deprecated by Google.

**Upstream API breakage is therefore the top operational risk**, and the
design mitigates it two ways: (1) capture the raw upstream payload durably
the moment it arrives, so a normalization or schema mistake is always
replayable and an outage never loses data already pulled; (2) back up early
and test restores (see phases), since providers only serve a limited history
window and manual logs are irreplaceable.

## Architecture

One Fly.io app, **one Machine, one volume**. Everything that touches the
SQLite file runs inside that single process, so all DB access is local
filesystem, never network file access. This is a deliberate constraint, not
a shortcut: Fly volumes attach to a single Machine with no built-in
cross-volume replication, and SQLite supports only one writer at a time.
Fly's answer for multi-machine SQLite is LiteFS, but it's beta, explicitly
unsupported by Fly, and incompatible with Machine autostop — not worth the
complexity for a single-user tool. Litestream (continuous single-node
backup to S3/B2-compatible storage) is the right tool for durability here
instead.

```
                      ┌─────────────────────────────────────────┐
                      │         Fly Machine (always-on)          │
                      │                                           │
  Claude (MCP) ──────▶│  FastAPI app                              │
                      │   ├── /mcp           (MCP tools)          │
  homelab cron ──────▶│   ├── /admin/import/garmin  (bearer auth) │
  (systemd timer)     │   ├── /admin/import/fitbit  (bearer auth) │
                      │   ├── /datasette/*   (mounted ASGI)       │
  you, ad hoc ────────▶│   └── /export/db    (snapshot download)  │
                      │                                           │
                      │   sqlite file (WAL mode) on Fly volume ───┼──▶ Litestream ──▶ B2/S3
                      └─────────────────────────────────────────┘
```

## Why this shape

- **Importers as HTTP-triggered, not cron-resident.** The homelab only
  needs a systemd timer that calls `curl -X POST .../admin/import/garmin`.
  All import logic, dependencies, and auth state live in one place (the Fly
  service), not split across homelab + cloud.
- **Datasette mounted in-process, not run separately.** Datasette is itself
  an ASGI app, so it can be mounted at a sub-path of the same FastAPI app
  (`app.mount("/datasette", datasette_app)`), reading the same file with no
  copying, no second process, no second deploy target.
- **MCP server in the same process** for the same reason — manual logging
  (caffeine, alcohol, meals) and ad hoc queries from Claude hit the same
  local file as everything else.
- **`/export/db` exists for offline deep analysis** (Jupyter/DuckDB
  correlation work) where you want a point-in-time snapshot on your own
  machine rather than live access. Use SQLite's online backup mechanism
  (`.backup` or `VACUUM INTO`) server-side to produce a consistent snapshot
  rather than streaming the live file mid-write.
- **WAL mode is mandatory** (`PRAGMA journal_mode=WAL`) so the importer
  (writer), the MCP server (reader/writer), and Datasette (reader) don't
  lock each other out within the same process.

## Repo structure (suggested)

```
health-data-service/
├── fly.toml
├── Dockerfile
├── pyproject.toml
├── schema.sql                  # versioned, see below
├── migrations/                 # 001_xxx.sql, 002_xxx.sql, applied in order
├── app/
│   ├── main.py                 # FastAPI app: mounts MCP, admin routes, Datasette
│   ├── db.py                   # connection helper, WAL setup, upsert helpers
│   ├── importers/
│   │   ├── garmin.py           # uses cyberjunky/python-garminconnect
│   │   └── fitbit.py           # pending overlap check, see Open Decisions
│   ├── mcp_server.py           # tool definitions
│   └── admin_routes.py         # /admin/import/*, /export/db, auth
├── analysis/
│   └── correlation.ipynb       # local notebook, pulls snapshot via /export/db
└── tests/
```

## Schema (draft — refine before first migration)

Designed so it works for a user with only Garmin, only Fitbit, or both —
adding a third source later (Oura, Whoop, ...) should mean new rows, not a
new table or new merge logic. Three layers: a source-agnostic raw metrics
table, structured per-source activity tables (these need real columns, not
EAV), and a normalized layer the analysis actually queries.

A fourth, lower layer sits under all of them: an append-only landing table
of the untouched upstream API responses. Everything else is derived from it,
so any parsing/normalization change can be replayed from stored payloads
without re-hitting (or depending on the continued availability of) the
provider API. This is the concrete form of the "capture raw durably" hedge
against upstream breakage.

```sql
-- append-only landing zone: the untouched upstream response for every fetch.
-- Never updated, only inserted. All parsing is replayable from here, and an
-- upstream outage can't lose data already pulled. Prune old rows manually if
-- it ever grows large — it won't, at one user's volume.
CREATE TABLE raw_import_payloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,           -- 'garmin' | 'fitbit' | ...
  endpoint TEXT NOT NULL,         -- which API call this came from
  date TEXT,                      -- the health date this payload is about, if applicable
  payload_json TEXT NOT NULL,     -- verbatim upstream response
  fetched_at TEXT NOT NULL
);
```

```sql
-- raw, source-agnostic: every daily scalar metric from every source lands
-- here as one row. Adding a source = more rows. Adding a metric = more
-- rows. No schema change either way.
CREATE TABLE raw_daily_metrics (
  date TEXT NOT NULL,
  source TEXT NOT NULL,        -- 'garmin' | 'fitbit' | ...
  metric TEXT NOT NULL,        -- 'resting_hr' | 'hrv' | 'vo2max' | 'sleep_score' |
                                -- 'sleep_duration_min' | 'sleep_deep_min' | 'sleep_light_min' |
                                -- 'sleep_rem_min' | 'sleep_awake_min' | 'body_battery_high' |
                                -- 'body_battery_low' | 'stress_avg' | 'stress_max' |
                                -- 'training_readiness' | 'acute_training_load' |
                                -- 'chronic_training_load' | 'training_load_ratio' | 'steps' | ...
  value REAL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (date, source, metric)
);

-- which source wins per metric when more than one has data for a day.
-- absent row = fall back to DEFAULT_SOURCE_PRIORITY (e.g. ['garmin','fitbit']).
-- a single-source user never needs to touch this table at all — the other
-- source just never has rows in raw_daily_metrics, so resolution falls
-- through to whichever source actually has data.
CREATE TABLE source_config (
  metric TEXT PRIMARY KEY,     -- matches `metric` values above, plus 'activities'
  canonical_source TEXT NOT NULL
);

-- activities stay structured per source — too many fields, and the merge
-- problem here is dedup (did both devices log the same run?), not
-- picking a winning scalar.
CREATE TABLE garmin_activities (
  activity_id TEXT PRIMARY KEY,
  date TEXT, start_time TEXT, type TEXT,
  duration_s INTEGER, distance_m REAL,
  avg_hr INTEGER, max_hr INTEGER,
  training_effect_aerobic REAL, training_effect_anaerobic REAL,
  calories INTEGER,
  raw_json TEXT,
  fetched_at TEXT
);

CREATE TABLE fitbit_activities (
  activity_id TEXT PRIMARY KEY,
  date TEXT, start_time TEXT, type TEXT,
  duration_s INTEGER, distance_m REAL,
  avg_hr INTEGER, max_hr INTEGER,
  calories INTEGER,
  raw_json TEXT,
  fetched_at TEXT
);

-- normalized, deduped activities — the table analysis/Datasette actually use
CREATE TABLE activities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT, start_time TEXT, type TEXT,
  duration_s INTEGER, distance_m REAL,
  avg_hr INTEGER, max_hr INTEGER,
  calories INTEGER,
  canonical_source TEXT,             -- which source's row this came from
  garmin_activity_id TEXT,           -- nullable, set if a Garmin row matched/contributed
  fitbit_activity_id TEXT            -- nullable, set if a Fitbit row matched/contributed
);

-- a *recommendation*, not a completed activity — kept source-tagged like
-- raw_daily_metrics in case another source ever offers something similar
CREATE TABLE suggested_workouts (
  date TEXT NOT NULL,
  source TEXT NOT NULL,              -- 'garmin' | ... (Fitbit may not have
                                      -- an equivalent — check when you get there)
  workout_type TEXT,                 -- e.g. 'recovery' | 'tempo' | 'long_run'
  description TEXT,
  target_duration_min REAL,
  target_intensity TEXT,
  raw_json TEXT,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (date, source)
);

-- manual logs (the part Claude writes to conversationally)
CREATE TABLE manual_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                       -- actual event time, not log time
  type TEXT NOT NULL,                     -- meal | caffeine | alcohol | note
  description TEXT,
  quantity REAL, unit TEXT,
  estimated_calories INTEGER,
  estimated_macros_json TEXT,             -- {protein_g, carbs_g, fat_g}
  confidence TEXT,                        -- e.g. 'photo_estimate' | 'user_confirmed'
  created_at TEXT NOT NULL
);

-- normalized layer the correlation analysis actually queries — built from
-- raw_daily_metrics + source_config by the normalization job, see below.
--
-- DELIBERATE TRADE-OFF: this is a *wide* table (one column per metric), which
-- contradicts the source/metric-agnostic design of raw_daily_metrics — adding
-- a metric here means a migration, not just new rows. That's accepted on
-- purpose: wide is far nicer for Datasette browsing and notebook joins than a
-- long/narrow table, and the metric set changes rarely. The raw layer stays
-- agnostic; only this presentation layer pays the migration cost.
--
-- `date` here is the canonical local health date — see the day-boundary note
-- below. All same-day joins and lag offsets in analysis are defined against it.
CREATE TABLE daily_metrics (
  date TEXT PRIMARY KEY,
  resting_hr REAL, hrv REAL,
  sleep_score REAL, sleep_duration_min REAL,
  body_battery_high REAL, body_battery_low REAL,
  stress_avg REAL,
  training_readiness REAL,
  vo2max REAL,
  steps REAL,
  caffeine_mg REAL,
  alcohol_units REAL,
  calories_estimated REAL,
  source_flags_json TEXT                  -- {metric: source_used, ...}, for traceability
);

CREATE TABLE import_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  started_at TEXT, finished_at TEXT,
  status TEXT, rows_upserted INTEGER, error TEXT
);
```

Migration convention: `schema.sql` is the v1 baseline; every change after
that ships as a new numbered file in `migrations/`, applied idempotently on
startup. Keep both in git.

## Day boundary and timezone convention (decide before phase 2)

The entire thesis — "alcohol the night before vs. HRV the next morning" — is
a set of date-offset self-joins, and those are only correct if every layer
agrees on what "a day" and "the night before" mean. This has to be pinned
down before the normalization job is written, because retrofitting it later
means reprocessing all history.

The rule: **store every instant in UTC, and record which timezone you were
actually in on each day; compute the "health day" boundary against that day's
zone.** This is more correct than a single fixed home timezone, which would
misattribute exactly the travel/DST days you most want in the analysis (your
"night" shifts by hours). The cost is one small table.

```sql
-- which timezone you were physically in on a given day, DERIVED from the
-- device data (local-offset / GPS on Garmin activity & sleep payloads), with
-- HOME_TZ as the fallback for days with no positioned data. Rebuilt by the
-- normalization job from raw_import_payloads, so it's reproducible.
CREATE TABLE day_timezone (
  date TEXT PRIMARY KEY,       -- canonical health date
  tz TEXT NOT NULL,            -- IANA zone (or fixed offset) resolved for the day
  source TEXT NOT NULL         -- 'garmin_activity' | 'garmin_sleep' | 'home_default' | ...
);
```

Rules:

- **All timestamps stored as UTC** (`manual_logs.ts`, `fetched_at`, etc.) —
  true instants, never local wall-clock. Keep the full timestamp; derive the
  health date, don't store only a date.
- **`day_timezone` is derived from device data, not entered by hand.** Garmin
  activity and sleep payloads carry the local UTC offset (and GPS on
  activities); the normalization job resolves each day's zone from those and
  writes it here with the source it came from. Days with no positioned data
  fall back to `HOME_TZ` (`source='home_default'`). Because it's rebuilt from
  `raw_import_payloads`, improving the derivation and replaying is free.
- **A record's health date is its UTC instant converted to that day's zone**
  (`day_timezone`, or `HOME_TZ` if absent) and truncated. This is one helper
  in `db.py`, used everywhere a date is assigned.
- **Sleep attribution follows the provider's own night-of date** (Garmin
  attributes a sleep session to a calendar date with its own rule); record
  the date the provider assigned rather than recomputing it, so sleep lines up
  with the morning-after recovery metrics the way the device intended.
- **`HOME_TZ` is a constant in `db.py`**, used only as the fallback when a day
  has no device-derived zone.

## Components

**1. Garmin importer** — built on `cyberjunky/python-garminconnect`
(actively maintained, the de facto standard wrapper, also what
garmin-mcp-dan is presumably built on — reuse its auth/session handling
rather than reinventing). Pulls a rolling 5–7 day window each run (not just
"yesterday") and writes rows into `raw_daily_metrics` with `source='garmin'`
(upsert via `ON CONFLICT(date, source, metric) DO UPDATE`), to catch
Garmin's late-arriving corrections to HRV status, sleep score, etc. Every
upstream response is first written verbatim to `raw_import_payloads` before
any parsing, so all of the below is re-derivable if parsing changes or the
API later goes dark. Also writes to `garmin_activities`, training load
metrics (acute/chronic load, load ratio — same `raw_daily_metrics` table,
just more metric keys), and `suggested_workouts` (Garmin's own daily workout
recommendation). If no
Garmin credentials are configured for the deployment, `/admin/import/garmin`
no-ops cleanly rather than erroring — the homelab cron can always hit both
import endpoints unconditionally.

**2. Fitbit importer** — same shape, `source='fitbit'`, writes to
`raw_daily_metrics` (including `vo2max`, which Fitbit also estimates) and
`fitbit_activities`. Same graceful no-op if unconfigured. Gated on the
overlap check in Open Decisions below.

**3. Normalization job** — runs after each import, rebuilds `day_timezone`,
`daily_metrics`, and `activities` from the raw tables. It resolves each day's
timezone first (from device offsets/GPS in the raw payloads, `HOME_TZ`
fallback), since date assignment for everything downstream depends on it:
  - *Scalar metrics*: for each `(date, metric)`, look up `canonical_source`
    in `source_config` (default: `DEFAULT_SOURCE_PRIORITY`, e.g.
    `['garmin', 'fitbit']`), take that source's value if present in
    `raw_daily_metrics`, otherwise fall back to whichever other source has
    a value for that day. Record which source won in `source_flags_json`.
    This is one generic function (`resolve_metric(date, metric)`), not
    per-column SQL, so it doesn't grow with each new source.
  - *Activities*: match rows across `garmin_activities`/`fitbit_activities`
    by `(date, type, start_time within ±15 min)`. Where both sources have a
    matching row, keep the canonical source's version per `source_config`
    (key `'activities'`) and record both source IDs for traceability. Where
    only one source has a row, keep it regardless of preference — presence
    beats preference, same philosophy as the scalar case.
  - A single-source deployment never populates `source_config` at all;
    everything resolves to "whichever source has data" by default.

**4. Admin/import endpoints** — `POST /admin/import/garmin`,
`POST /admin/import/fitbit`, bearer-token auth (separate secret from the
MCP and export tokens), each followed by the normalization step, logged to
`import_runs`. Called by a homelab systemd timer once nightly.

**5. MCP server** — tools:
  - `log_meal(ts?, description, estimated_calories?, estimated_macros?, confidence)`
  - `log_caffeine(ts?, description, amount_mg?)`
  - `log_alcohol(ts?, description, units?)`
  - `get_logs(start_date, end_date, type?)`
  - `get_daily_metrics(start_date, end_date)`
  - `get_suggested_workout(date?)` — Garmin's recommendation plus the
    training load context behind it, for your existing weather+workout
    email routine to consume instead of/alongside querying Garmin directly
  - `get_source_config()` / `set_source_preference(metric, source)` — lets
    you say "use Fitbit's vo2max, Garmin's sleep" conversationally instead
    of editing the `source_config` table by hand

  Note: this server stays a data layer, not the orchestrator — it doesn't
  fetch weather or send the email itself. Your existing Claude routine
  keeps owning that, just pointed at `get_daily_metrics` /
  `get_suggested_workout` here instead of (or alongside) garmin-mcp-dan
  directly, which also means it picks up alcohol/caffeine/sleep context
  for free without the routine itself needing new logic.

  The MCP server never handles image bytes. Claude does the visual
  estimation of a food photo itself and calls `log_meal` with the
  structured result — the server just needs to accept and store structured
  fields. Implement against whatever transport/auth the current MCP Python
  SDK and Claude's remote-MCP connector spec require — check current docs
  before building, this has been a moving target.

**6. Datasette** — mounted at `/datasette` as a sub-application of the same
FastAPI app, pointed at the same file. Free browsing, SQL queries, and basic
charting (e.g. `datasette-vega`) with no extra deployment. Two hard
requirements, because this process also holds write paths: (a) open the file
**read-only / immutable** for Datasette's connection so no query path can
write, and (b) **put it behind auth** — the plan lists bearer tokens for
admin/export/MCP but not Datasette; it must not be left open, since one
misconfiguration exposes arbitrary SQL over your entire health history.
Gate it with the same reverse-proxy/bearer scheme as the other endpoints (or
`datasette` auth plugin), and decide this before deploy, not after.

**7. `/export/db`** — bearer-token-protected endpoint that runs
`VACUUM INTO` (or `.backup`) to produce a consistent snapshot and streams
it back, for pulling into a local Jupyter/DuckDB session for the actual
correlation work.

**8. Analysis notebook** — local, not deployed. Pulls a snapshot via
`/export/db`, joins `daily_metrics` against itself with date offsets (e.g.
alcohol units the night before vs. HRV the next morning), computes lagged
correlations. The qs_ledger project's "combine into unified dataframe, then
correlate" notebook pattern is a reasonable template to start from rather
than designing the methodology from scratch.

## Deployment notes

- `fly volumes create healthdata --size 1` (1GB is generous for this data
  volume; resize later if needed)
- Single Machine, 
- `PRAGMA journal_mode=WAL;` on every connection at startup
- Three separate bearer tokens as Fly secrets: one for admin/import
  endpoints, one for export, MCP auth per current connector spec
- Litestream sidecar replicating the volume to Backblaze B2 (or S3) on a
  short interval, as the actual disaster-recovery story

## Build phases (for Claude Code, roughly in order)

0. Repo scaffold, `schema.sql`, `db.py` with WAL + upsert helpers,
   `fly.toml` + Dockerfile, volume created
1. Garmin importer (writes `raw_import_payloads` first, then
   `raw_daily_metrics` + `garmin_activities`) + `/admin/import/garmin`,
   tested against a local file before touching Fly
1b. **Litestream backup + a tested restore, as soon as phase 1 produces real
   rows.** Moved up front deliberately: providers only serve a limited history
   window and manual logs are irreplaceable, so the moment real data exists it
   must be backed up. Replicate to B2/S3 *and* actually restore into a scratch
   volume and diff — an untested backup isn't a backup.
2. Normalization job (`day_timezone` resolution, `resolve_metric`, activity
   dedup) + `daily_metrics`/`activities` build, tested with synthetic
   Garmin-only data first. Build this **single-source-clean** — the merge
   machinery (`source_config`, dedup, multi-source `resolve_metric`) is real
   but stays exercised only by synthetic tests until there's actual
   overlapping data; don't gold-plate it against a Fitbit you may never enable.
3. Fitbit importer (writes `raw_daily_metrics` with `source='fitbit'` +
   `fitbit_activities`) + `/admin/import/fitbit` — **genuinely deferred and
   conditional**: only build it after confirming (a) you actually wear a
   second device that produces overlapping data worth merging, and (b) it's
   not redundant with `fitbit2garmin` (see below). If neither holds, skip it;
   the schema already supports adding it later with zero migration.
4. MCP server tools, including `get_source_config`/`set_source_preference`,
   wired to the same `db.py`
5. Mount Datasette + build `/export/db`
6. Deploy to Fly (Machine + volume + WAL), promote the Litestream
   config validated in 1b to the production volume, and re-run the
   restore test against the deployed replica; point homelab systemd
   timer at the admin endpoints
7. Build the local correlation notebook against a pulled snapshot

## Open decisions before/while building

- **Manual log estimation precision**: how much macro detail to ask
  Claude to estimate from a photo (calories only vs. full macro
  breakdown), and whether low-confidence estimates should be flagged
  differently in `daily_metrics` aggregation.
- **`DEFAULT_SOURCE_PRIORITY`**: what order to fall back to when a metric
  has no explicit `source_config` row and both sources have data — pick
  something now (e.g. Garmin first, since it's your primary watch) so the
  normalization job has a default on day one, and override per-metric
  (vo2max → Fitbit, etc.) as you actually compare the two against each
  other.
- **Activity-dedup tolerance**: is a ±15 minute start-time window the
  right match criterion across Garmin/Fitbit, or should it also weight
  duration/distance similarity — worth checking against real overlapping
  data once both importers are running rather than guessing upfront.
- **MCP auth mechanism**: confirm current Claude remote-MCP connector
  auth requirements before implementing `mcp_server.py` — verify against
  current docs rather than assuming a specific flow.

