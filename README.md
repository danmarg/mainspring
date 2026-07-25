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
| `log_note(description)` | Log a free-text note |
| `amend_log(log_id, ...)` | Correct a previous log entry |
| `delete_log(log_id)` | Delete a log entry |

All logging tools accept an optional `ts` parameter (UTC ISO-8601) to back-date entries.

### Query tools

| Tool | What it returns |
|---|---|
| `get_logs(start_date, end_date, type?)` | Raw log entries (meals, caffeine, alcohol, notes) |
| `get_daily_metrics(start_date, end_date)` | HRV, sleep, training load, nutrition totals by date |
| `get_workout_context(date?)` | Everything needed for workout planning in one call: today's HRV + trend, training stress balance (TSB/form), week volume vs targets, next goal event, yesterday's RPE, recent activities, Garmin's suggested workout, and personalized insights from historical correlations |
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

