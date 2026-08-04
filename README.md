# Mainspring

A self-hosted personal health data service. Ingests biometrics from Garmin and Google Health, accepts manual nutrition/caffeine/alcohol logs via Claude (MCP), and exposes a dashboard for trend analysis and behavior-to-recovery correlations.

Everything runs in a single Docker container with a single SQLite file — no external database, no cloud dependency by default.

---

## Screenshots

### Today — readiness score + alertness forecast

![Today overview](docs/screenshots/overview.png)

### Trends — HRV, sleep, training load, resting HR

![Trends](docs/screenshots/trends.png)

### Behavior — alcohol & caffeine vs. HRV delta

![Behavior](docs/screenshots/behavior.png)

### Nutrition — calories and macros over time

![Nutrition](docs/screenshots/nutrition.png)

---

## Quick start

**Prerequisites:** Docker + Docker Compose, `openssl` on your PATH.

```bash
git clone https://github.com/danmarg/mainspring
cd mainspring

# 1. Generate .env with random bearer tokens
bash scripts/generate_config.sh

# 2. Set HOME_TZ and add Garmin credentials (see Garmin setup below)
$EDITOR .env

# 3. Start
docker compose up -d

# 4. Open the dashboard — authenticate with DATASETTE_TOKEN from .env
open http://localhost:8080/dashboard
```

The importer service runs both importers once per hour. Unconfigured sources are silently skipped.

---

## Configuration

All config lives in `.env`. See `.env.example` for the full reference.

### Required

| Variable | Purpose |
|---|---|
| `ADMIN_TOKEN` | Import endpoints (`/admin/import/*`) |
| `DATASETTE_TOKEN` | Dashboard login + Datasette SQL explorer |
| `EXPORT_TOKEN` | `/export/db` snapshot download |
| `HOME_TZ` | IANA timezone name, e.g. `America/New_York` |

`scripts/generate_config.sh` generates a `.env` with random tokens for the first three.

### Garmin setup

Two auth methods — pick one:

**Option A — session tokens (recommended, avoids 2FA friction):**

```bash
python3 -c "
from garminconnect import Garmin
g = Garmin()
g.login('your@email.com', 'password')
print(g.garth.dumps())
"
```

Paste the printed JSON string as `GARMINTOKENS` in `.env`. Tokens are long-lived; re-run if they expire.

**Option B — email + password:**

```env
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=secret
```

This may fail if Garmin requires 2FA for your account.

### Google Health setup (optional)

Google Health adds sleep stages and other metrics not available from Garmin directly. Skip this if you only have a Garmin device.

1. Go to [Google Cloud Console](https://console.cloud.google.com) → APIs & Services → Credentials
2. Create an **OAuth 2.0 Client ID** (type: Web application)
3. Add an authorized redirect URI:
   - Local: `http://localhost:8080/google_health_callback`
   - Remote: `https://your-domain/google_health_callback`
4. Copy the client ID and secret into `.env`:
   ```env
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   ```
5. Complete the OAuth flow:
   ```bash
   python scripts/google_health_get_tokens.py \
     --base-url http://localhost:8080 \
     --token $ADMIN_TOKEN
   ```
   This opens a browser window, completes the OAuth consent, and stores the tokens in the app's database. Re-run to refresh.

### Optional features

| Feature | Env var(s) required |
|---|---|
| MCP server (Claude nutrition logging) | `MCP_TOKEN` |
| Litestream real-time DB backup | `LITESTREAM_REPLICA_URL` |
| Fitbit | `FITBIT_CLIENT_ID`, `FITBIT_CLIENT_SECRET` |

**Litestream** replicates the SQLite database to S3/B2/R2 in real time. Set `LITESTREAM_REPLICA_URL` in `.env`:

```env
# Backblaze B2:
LITESTREAM_REPLICA_URL=s3://mybucket/health.db?endpoint=https://s3.us-east-005.backblazeb2.com&access-key-id=KEY&secret-access-key=SECRET

# AWS S3 (uses default credential chain):
LITESTREAM_REPLICA_URL=s3://mybucket/health.db
```

---

## Claude integration (MCP)

`MCP_TOKEN` is a bearer token you choose (or let `scripts/generate_config.sh` generate). When it is set, the app exposes an MCP server at `/mcp`.

To connect it to Claude:

1. In Claude, go to **Settings → Integrations** and add a remote MCP server
2. URL: `https://your-app/mcp` (or `http://localhost:8080/mcp` locally)
3. Auth: Bearer token — paste the value of `MCP_TOKEN` from your `.env`

Once connected, Claude can log meals, caffeine, alcohol, weight, and blood pressure directly from conversation, and query your health history.

### Logging tools

| Tool | What it does |
|---|---|
| `log_meal(description, estimated_calories?, estimated_macros?, confidence?)` | Log a meal; Claude can estimate calories and macros from a photo or description |
| `log_caffeine(description, amount_mg?)` | Log a coffee or other caffeine source |
| `log_alcohol(description, units?)` | Log alcohol in standard units |
| `log_weight(kg)` | Log a weight measurement |
| `log_blood_pressure(systolic, diastolic, pulse?)` | Log a BP reading |
| `log_rpe(value, activity_type?)` | Log perceived exertion after a workout (1–10) |
| `log_hydration(ml)` | Log fluid intake; also pushed to Garmin Connect if configured |
| `log_soreness(body_part, severity, notes?)` | Log soreness/minor injury (severity 1–10); surfaces in `get_workout_context` for 3 days |
| `log_note(description)` | Log a free-text note |
| `amend_log(log_id, ...)` | Correct a previous log entry |
| `delete_log(log_id)` | Delete a log entry |

All logging tools accept an optional `ts` parameter (UTC ISO-8601) to back-date entries.

### Query tools

| Tool | What it returns |
|---|---|
| `get_logs(start_date, end_date, type?)` | Raw log entries (meals, caffeine, alcohol, notes) |
| `get_daily_metrics(start_date, end_date)` | HRV, sleep (incl. sleep-specific respiration, skin temp deviation), training load, hydration, lactate threshold, FTP, recovery time, nutrition totals by date |
| `get_workout_context(date?)` | Everything needed for workout planning in one call: today's HRV + trend, composite readiness score, training stress balance (TSB/form) and ACWR, personalized HR zones, recovery time, sleep regularity, week volume vs targets, next goal event, yesterday's RPE, recent soreness, recent activities, Garmin's suggested workout, and personalized insights from historical correlations |
| `get_suggested_workout(date?)` | Garmin's raw workout suggestion for a date |
| `get_correlations(input_metric, output_metric, lag?)` | Pearson/Spearman correlation between any two metrics with a configurable day lag — e.g. `alcohol_units` → `hrv` at lag 1 reveals last night's drinking vs this morning's recovery |
| `get_training_goals()` | Current weekly training targets |
| `set_training_goal(metric, value, unit?)` | Set a weekly target (e.g. `weekly_duration_min = 300`) |
| `add_training_event(date, type, description, goal_description?)` | Add a goal race or event |
| `list_training_events(status?)` | List upcoming/completed training events |
| `get_source_config()` | See which data source is canonical for each metric |
| `set_source_preference(metric, source)` | Override source priority for a metric |

---

## Morning workout planning webhook

The app fires a webhook after each morning import when today's sleep score arrives for the first time. This lets you trigger an automated Claude routine that reads your overnight recovery data and plans the day's training before you've even looked at your phone.

### Setup

1. Go to [claude.ai/code/routines](https://claude.ai/code/routines) and click **New Routine**
2. Choose **API trigger** — Claude will generate a webhook URL and a secret token
3. Paste your prompt (see example below)
4. Copy the webhook URL and secret into your `.env`:

```env
MORNING_WEBHOOK_URL=https://...   # the URL Claude gave you
MORNING_WEBHOOK_SECRET=...        # the secret Claude gave you
```

The app sends a `POST {}` to `MORNING_WEBHOOK_URL` with `Authorization: Bearer <MORNING_WEBHOOK_SECRET>` after each morning import when the day's sleep score first arrives.

### Example prompt

```
Call get_workout_context() on the Mainspring MCP server, then recommend
today's workout and explain your reasoning. If I have a goal event coming
up, factor in the training block. Reply in 3–4 sentences.
```

`get_workout_context()` returns HRV, sleep score, TSB (form), week volume so far, yesterday's RPE, the next upcoming race, Garmin's suggested workout, and correlation-derived insights about which behaviors most affect your recovery. Claude synthesises all of that into a single actionable recommendation.

You can extend the prompt to also check weather, suggest a specific route, or send the result somewhere (email, Slack, push notification).

---

## Manual imports

Imports run automatically every hour via the `importer` service. To trigger one immediately:

```bash
# Garmin
curl -X POST http://localhost:8080/admin/import/garmin \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# Google Health
curl -X POST http://localhost:8080/admin/import/google_health \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Each import pulls a rolling 7-day window to catch late-arriving Garmin corrections (HRV, sleep scores).

---

## Dashboard pages

| Page | URL | What it shows |
|---|---|---|
| Today | `/dashboard` | Readiness score, info strip, today's nutrition, alertness forecast |
| Trends | `/dashboard/trends` | HRV, sleep, training load, resting HR, body battery over time |
| Behavior | `/dashboard/behavior` | Alcohol and caffeine per day + DoW averages; HRV delta diverging bars |
| Nutrition | `/dashboard/nutrition` | Calories and macros per day, DoW macro averages |
| Activities | `/dashboard/activities` | Activity log table |
| Vitals | `/dashboard/vitals` | Blood oxygen, respiration, weight |

The time range selector (7d / 30d / 90d / 180d / 360d) persists across page navigation.

Datasette (raw SQL over all tables) is available at `/datasette`, gated by the same `DATASETTE_TOKEN`. See the [Datasette section](#datasette--sql-exploration) below for example correlation queries.

---

## Architecture

- **Single SQLite file** at `DB_PATH` (default `/data/health.db`), WAL mode
- **FastAPI** app serves the dashboard, admin import routes, optional MCP server, and Datasette
- `raw_import_payloads` — append-only landing zone written before any parsing; everything else is derived and replayable
- `raw_daily_metrics(date, source, metric, value)` — provider-agnostic EAV table; adding a new source = new rows, no schema change
- `daily_metrics` — normalized wide table rebuilt after each import; this is what the dashboard queries
- `manual_logs` — write target for all MCP nutrition/caffeine/alcohol logging tools

---

## Which metrics come from which platform

Both importers write into the same `raw_daily_metrics` table, so most biometrics are available regardless of source — but a few are platform-specific. This matters for `source_config`/`set_source_preference` (there's nothing to choose between if only one source provides a metric) and for readiness scoring (Garmin-only signals silently drop out of the composite score if you're not wearing a Garmin — see [Configuration](#configuration)).

| Metric | Garmin | Google Health |
|---|:---:|:---:|
| HRV | ✅ (already overnight-only) | ✅ (recomputed from overnight intraday samples when available — see below) |
| Resting heart rate | ✅ (computed from raw intraday HR, not either vendor's own RHR — see below) | ✅ |
| Sleep score | ✅ (native) | ✅ (synthesized from stages if not provided) |
| Sleep duration / stages (deep, REM, light, awake) | ✅ | ✅ |
| Steps, active zone minutes, calories | ✅ | ✅ |
| SpO2, breathing rate (respiration) | ✅ | ✅ |
| VO2max | ✅ | ✅ |
| **Training readiness** (Garmin's own vendor score) | ✅ | ❌ |
| **Acute/chronic training load (ATL/CTL) → TSB and ACWR** | ✅ | ❌ |
| **Body Battery** (high/low) | ✅ | ❌ |
| **Stress score** (avg/max) | ✅ | ❌ |
| **Suggested workout** | ✅ | ❌ |
| Blood pressure | ✅ (read + write-back to Garmin Connect) | ❌ |
| **Hydration** | ✅ (read + write-back; also loggable via `log_hydration`) | ❌ |
| **Lactate threshold HR/pace, cycling FTP** | ✅ | ❌ |
| **Recovery time** | ✅ | ❌ |
| **Skin temperature deviation** | ✅ (compatible devices only — Venu 3, epix Pro, fēnix 7 Pro+) | ✅ (compatible devices only; field names unverified — see caveat below) |
| Sleep-specific respiration (distinct from all-day `breathing_rate`) | ✅ | ❌ (falls back to all-day average) |
| HR zones (derived from `max_hr`, standard %HRmax bands — not vendor-scraped) | ✅ (max_hr sourced from Garmin today) | ❌ (would work automatically if a source ever provides `max_hr`) |
| Sleep regularity (bed/wake-time consistency) | ✅ | ✅ (fully derived from `sleep_wake_hour` + duration, already shared) |
| Soreness/injury logging (`log_soreness`) | ✅ (manual — not platform-dependent) | ✅ (manual — not platform-dependent) |

Garmin is the only source for training-load-derived signals (TSB, ACWR, `training_readiness`) and the Firstbeat-modeled metrics above (lactate threshold, FTP, recovery time) because Google Health/Health Connect has no equivalent API — there's no on-device Banister impulse-response model or sports-science engine to read. Hydration *does* have a Health Connect record type in principle, so a Google Health importer could pick it up later — it's just not wired up yet. The composite `readiness` score (see [`get_workout_context`](#query-tools)) degrades gracefully without any of these: it renormalizes across whatever's available (HRV, sleep, resting HR), it just won't include the training-load components. If you only have a Garmin-compatible device, none of this matters — Garmin alone covers everything in this table.

**Resting heart rate is computed, not vendor-reported.** Garmin's `restingHeartRate` is an overnight minimum; Google Health/Health Connect's `dailyRestingHeartRate` is closer to waking RHR and reads ~10bpm higher for the same person on the same night — so picking one canonical source per `source_config` would still produce a discontinuity if you ever switch, or an apples-to-oranges trend across a Garmin-then-Fitbit history. Instead, `daily_metrics.resting_hr` is computed from raw 1-minute HR samples in `intraday_hr` (mean of the lowest 5% of samples in the overnight window), which both importers populate the same way. This only works for dates with intraday HR on file — both importers pull it for just the last ~2 days on a normal rolling import (a full month of backfill is ~50-100 extra API pages, see `app/importers/garmin.py`/`google_health.py`), so older dates fall back to whichever vendor's RHR `resolve_metric` finds via the normal source-priority.

**HRV has the same problem on Google Health's side only.** Garmin's `hrv` is already overnight-only (`hrvSummary.lastNight`), but Google Health/Health Connect's `daily-heart-rate-variability` rollup is a 24h aggregate, not sleep-window-specific. Google Health separately provides real overnight RMSSD samples (`intraday_hrv`, ~5-min resolution) via the same paging fetch used for the dashboard's intraday HRV data — when Google Health's daily rollup is what gets picked, `hrv` is recomputed from those samples instead. Garmin's figure is left untouched since it doesn't have this problem. Both this and the RHR fix above track *which* computation method produced each day's value in `source_flags_json`, and `readiness_from_db` only builds its 7-day rolling baseline from days using the same method as today — mixing methodologies through the readiness score's ratio-based components would otherwise read as a manufactured recovery swing on the day the method changes, not real signal.

**Field names to double-check after your first real import**, since several of the metrics above come from undocumented parts of each API and were implemented defensively (best-effort field name guesses, degrading to a silent `NULL` rather than an error if wrong) rather than against verified documentation: Garmin `recoveryTime` (training readiness), `avgSleepRespirationValue` and `skinTempDataDTOList`/`skinTempCelsius` (sleep endpoint), and the FTP response shape; Google Health's `skin-temperature` data type and its delta field names entirely (Health Connect's `SkinTemperatureRecord` support is new and may not match the guessed shape). If any of these stay `NULL` for you, check the corresponding row in `raw_import_payloads` — the untouched upstream response is always captured before parsing, so fixing the parser later doesn't require re-fetching anything.

---

## Datasette / SQL exploration

Datasette is available at `/datasette` (authenticate with `DATASETTE_TOKEN`). It gives you a full SQL interface over every table — useful for ad-hoc correlation queries that go beyond what the dashboard and MCP tools expose.

### Useful queries

**Alcohol the night before vs. next-morning HRV** (lag-1 correlation):
```sql
SELECT
  a.date              AS night,
  a.alcohol_units,
  b.hrv               AS next_hrv,
  b.hrv - AVG(b.hrv) OVER (
    ORDER BY b.date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW
  )                   AS hrv_vs_14d_avg
FROM daily_metrics a
JOIN daily_metrics b ON b.date = DATE(a.date, '+1 day')
WHERE a.alcohol_units IS NOT NULL AND b.hrv IS NOT NULL
ORDER BY a.date DESC;
```

**Sleep score vs. next-day training readiness:**
```sql
SELECT
  a.date, a.sleep_score, a.sleep_duration_min,
  b.training_readiness AS next_readiness
FROM daily_metrics a
JOIN daily_metrics b ON b.date = DATE(a.date, '+1 day')
WHERE a.sleep_score IS NOT NULL AND b.training_readiness IS NOT NULL
ORDER BY a.date DESC;
```

**Caffeine timing vs. sleep score** — does late caffeine hurt sleep?
```sql
SELECT
  DATE(ts) AS log_date,
  MAX(CAST(strftime('%H', ts) AS INTEGER)) AS latest_caffeine_hour,
  SUM(quantity) AS total_mg,
  m.sleep_score AS next_sleep_score
FROM manual_logs
JOIN daily_metrics m ON m.date = DATE(DATE(ts), '+1 day')
WHERE type = 'caffeine'
GROUP BY DATE(ts)
ORDER BY log_date DESC;
```

**Weekly training load trend:**
```sql
SELECT
  strftime('%Y-W%W', date) AS week,
  ROUND(AVG(acute_training_load), 1)  AS atl,
  ROUND(AVG(chronic_training_load), 1) AS ctl,
  ROUND(AVG(training_load_ratio), 2)  AS tsb
FROM daily_metrics
WHERE acute_training_load IS NOT NULL
GROUP BY week
ORDER BY week DESC
LIMIT 12;
```

**All manual logs with daily HRV for that day:**
```sql
SELECT
  DATE(l.ts) AS date,
  l.type, l.description, l.quantity, l.unit,
  m.hrv, m.sleep_score, m.training_readiness
FROM manual_logs l
LEFT JOIN daily_metrics m ON m.date = DATE(l.ts)
ORDER BY l.ts DESC
LIMIT 100;
```

These queries run directly in the Datasette UI — no setup required. You can also download a consistent DB snapshot from `/export/db` (requires `EXPORT_TOKEN`) and run heavier analysis locally in a notebook.

---

## Deploying to Fly.io

The `fly.toml` and `scripts/fly-deploy.sh` deploy to [Fly.io](https://fly.io). This is optional — Docker Compose is the primary path.

```bash
fly auth login
fly apps create mainspring       # or your own name
fly volumes create health_data --size 10 --region iad

# Mirror your .env as Fly secrets:
fly secrets set \
  ADMIN_TOKEN=... \
  DATASETTE_TOKEN=... \
  EXPORT_TOKEN=... \
  MCP_TOKEN=... \
  APP_BASE_URL=https://your-app.fly.dev \
  HOME_TZ=Europe/Berlin \
  GARMINTOKENS='...' \
  GOOGLE_CLIENT_ID=... \
  GOOGLE_CLIENT_SECRET=...

./scripts/fly-deploy.sh
```

The app auto-stops when idle and wakes on the first incoming request.

### Scheduled imports on Fly.io

`fly-deploy.sh` manages a dedicated Fly Machine that runs the importers hourly. On first run it creates the scheduler machine automatically; on subsequent deploys it updates it to the new image. No manual setup required.

The scheduler calls the app via its public URL so the Fly proxy wakes the auto-stopped app machine on each run.

**Fallback: GitHub Actions**

The included `.github/workflows/import.yml` provides a secondary hourly trigger via GitHub Actions. To enable it, add two secrets to your repository (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `MAINSPRING_URL` | Your app's public URL, e.g. `https://your-app.fly.dev` |
| `MAINSPRING_ADMIN_TOKEN` | The value of `ADMIN_TOKEN` from your `.env` |

The workflow also has a manual trigger that lets you run a backfill for a specific date range.

