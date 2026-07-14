# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A self-hosted personal health data service deployed to Fly.io. It ingests biometrics from Garmin and Fitbit (HRV, sleep, stress, training readiness, activities), accepts manual nutrition/caffeine/alcohol logs via Claude (MCP), and enables lag-correlation analysis between behavior and recovery metrics.

**Scope of "provider-independent":** this owns *retention and analysis* — the full history in a format we control, plus correlations the vendor apps won't do. It does **not** own *collection*: data still originates on Garmin/Fitbit devices and flows through their (partly unofficial) cloud APIs, so upstream API breakage is the top operational risk. Two mitigations are baked into the design: capture the untouched upstream payload durably before parsing (`raw_import_payloads`), and back up + test-restore early.

The full build plan is in `plan.md`.

## Architecture constraints

**Single Machine, single SQLite file.** Everything runs in one Fly.io Machine with one persistent volume. No LiteFS, no multi-writer setup. Litestream handles disaster recovery by replicating to B2/S3.

**WAL mode is mandatory.** Set `PRAGMA journal_mode=WAL;` on every connection at startup so the importer (writer), MCP server (reader/writer), and Datasette (reader) don't block each other.

**Datasette and MCP server mount in-process.** Both run as sub-applications of the same FastAPI app — Datasette as a mounted ASGI app at `/datasette`, the MCP server at `/mcp`. They share the same SQLite file with no inter-process communication. Because this process also holds write paths, Datasette must open the file **read-only / immutable** and sit **behind auth** — never leave `/datasette` open, since one misconfig exposes arbitrary SQL over the entire health history.

**Store instants in UTC; derive the "health day" from a per-day timezone.** Every timestamp is stored UTC. A `day_timezone(date, tz, source)` table, **derived by the normalization job from device offset/GPS in the raw payloads** (with `HOME_TZ` as fallback), defines each day's zone; a record's health date is its UTC instant converted to that day's zone. This handles travel/DST instead of assuming a fixed home offset — which matters because the whole analysis is date-offset self-joins ("alcohol the night before vs. HRV the next morning"). Sleep keeps the provider's own night-of date.

## Repo structure (target)

```
health-data-service/
├── fly.toml
├── Dockerfile
├── pyproject.toml
├── schema.sql                  # v1 baseline — run once on fresh DB
├── migrations/                 # 001_xxx.sql etc., applied idempotently on startup
├── app/
│   ├── main.py                 # FastAPI app: mounts MCP, admin routes, Datasette
│   ├── db.py                   # connection helper, WAL setup, upsert helpers
│   ├── importers/
│   │   ├── garmin.py           # uses cyberjunky/python-garminconnect
│   │   └── fitbit.py
│   ├── mcp_server.py           # MCP tool definitions
│   └── admin_routes.py         # /admin/import/*, /export/db, bearer auth
├── analysis/
│   └── correlation.ipynb       # local only; pulls snapshot via /export/db
└── tests/
```

## Build phases (current status: not started)

0. Repo scaffold, `schema.sql`, `db.py`, `fly.toml` + Dockerfile, Fly volume
1. Garmin importer (writes `raw_import_payloads` first, then parsed tables) + `POST /admin/import/garmin`
1b. Litestream backup + a **tested restore**, as soon as phase 1 produces real rows (irreplaceable data → back up early; an untested backup isn't a backup)
2. Normalization job (`day_timezone` resolution, `resolve_metric`, activity dedup) → `daily_metrics` / `activities`. Build **single-source-clean**; merge machinery stays test-only until real overlapping data exists
3. Fitbit importer + `POST /admin/import/fitbit` — **genuinely deferred/conditional**: only if a second device produces overlapping data worth merging and it's not redundant with `fitbit2garmin`. Schema already supports adding it with zero migration
4. MCP server tools + `get_source_config` / `set_source_preference`
5. Datasette mount + `/export/db` endpoint
6. Fly deploy (Machine + volume + WAL); promote the Litestream config validated in 1b and re-run the restore test against the deployed replica
7. Local correlation notebook

## Key schema decisions

- **`raw_import_payloads`** — append-only landing zone for untouched upstream API responses, written before any parsing. Everything else is derived from it, so parse/normalization changes are replayable and an outage never loses pulled data.
- **`raw_daily_metrics(date, source, metric, value)`** — all scalar biometrics land here regardless of source. Adding a new source or metric = new rows, no schema changes.
- **`daily_metrics`** — the normalized, deduped view rebuilt after each import. This is what analysis queries. Deliberately a **wide** table (one column per metric) for Datasette/notebook ergonomics, accepting that a new metric here needs a migration — the raw layer stays agnostic.
- **`day_timezone`** — derived per-day zone (see architecture constraints); rebuilt from raw payloads by the normalization job.
- **`source_config`** — per-metric canonical source preference. Absent row = fall back to `DEFAULT_SOURCE_PRIORITY` (Garmin first). Single-source deployments never need to touch this.
- Activity dedup matches across `garmin_activities` / `fitbit_activities` by `(date, type, start_time ±15 min)`.
- `manual_logs` is the write target for all MCP logging tools (meal, caffeine, alcohol, note).

## Auth

Three separate bearer tokens as Fly secrets:
- Admin/import endpoints (`/admin/import/*`)
- Export endpoint (`/export/db`)
- MCP server (per current Claude remote-MCP connector spec — **verify current docs before implementing**, this has been a moving target)

`/datasette` must also be gated (reverse-proxy/bearer or a `datasette` auth plugin) — it is not public.

Import endpoints gracefully no-op if the corresponding source's credentials are not configured, so the homelab cron can always call both unconditionally.

## MCP tools

```
log_meal(ts?, description, estimated_calories?, estimated_macros?, confidence)
log_caffeine(ts?, description, amount_mg?)
log_alcohol(ts?, description, units?)
get_logs(start_date, end_date, type?)
get_daily_metrics(start_date, end_date)
get_suggested_workout(date?)
get_source_config()
set_source_preference(metric, source)
```

The MCP server is a data layer only — it does not fetch weather or send emails. Claude's existing weather+workout email routine calls `get_daily_metrics` / `get_suggested_workout` here instead of querying Garmin directly. Claude handles visual estimation for food photos and calls `log_meal` with the structured result; the server never handles image bytes.

## Garmin library

Use `cyberjunky/python-garminconnect` — the de facto standard Python wrapper. Pull a rolling 5–7 day window per import run (not just "yesterday") to catch Garmin's late-arriving corrections to HRV status, sleep score, etc. Write with `ON CONFLICT(date, source, metric) DO UPDATE`.

## `/export/db`

Use `VACUUM INTO` (or `.backup`) server-side to produce a consistent snapshot before streaming. Do not stream the live WAL file.

## Open decisions (resolve before implementing the affected component)

- **MCP auth mechanism** — verify current Claude remote-MCP connector requirements before writing `mcp_server.py`.
- **Manual log precision** — calories only vs. full macro breakdown from photo estimates; whether low-confidence estimates should affect `daily_metrics` aggregation differently.
- **`DEFAULT_SOURCE_PRIORITY`** — pick an order on day one (e.g. `['garmin', 'fitbit']`); override per-metric via `source_config` as real overlapping data reveals which source is more accurate.
- **Activity dedup tolerance** — ±15 min start-time window is the initial guess; validate against real overlapping data once both importers run.
