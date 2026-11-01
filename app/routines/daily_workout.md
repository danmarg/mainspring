# Daily workout routine

Served by `get_routine_instructions("daily_workout")` and followed by the webhook-fired
routine. Personal details (recipient address, athlete name) are NOT in this file —
the repo is public. The routine's own short prompt supplies them; below, "the athlete"
and "the recipient" refer to those values.

## Step 0: Wake check — run this first, before anything else

Call `Mainspring:get_daily_metrics` with start_date and end_date both set to today's date in the athlete's local timezone (from the routine's prompt). Look at today's sleep entry:

- If there is no sleep entry for today, or it looks incomplete/in-progress (e.g. missing total sleep duration or sleep end time), the athlete is not awake yet. **Exit immediately** — do not run any of the steps below, do not send or draft anything, do not log anything. This should be a silent no-op.
- If today's sleep data is present and looks finalized, continue to Step 0.5.

## Step 0.5: Claim today's run (duplicate guard)

This routine is fired by a webhook that can fire more than once a day, sometimes at the same moment. Call `Mainspring:claim_daily_recommendation(date=<the same local date used in Step 0>)` (kind defaults to "daily_workout"). Always pass the date: the server's own default is its UTC date, which can differ from the athlete's local date around midnight.

- If it returns `claimed: false`, another run already owns today. **Exit immediately and silently.**
- If it returns `claimed: true`, continue. If anything later fails **before the email is sent**, call `Mainspring:release_daily_recommendation(date=<same date>)` before exiting so a later run can retry. Never release after sending.

Do not use a Gmail "already sent?" search as the guard: concurrent runs all pass it before either sends.

## Instructions

You are generating today's workout recommendation for the athlete. Do not assume any race, goal time, or training phase — derive them fresh from Mainspring each run.

### 1. Gather data
- Read the `mainspring://guide` resource. Its rules (including "read consistency_checks first" and "protect quality sessions") are binding.
- `Mainspring:get_workout_context` — today's metrics, readiness, form_pct, HR zones (`hr_zones` + `hr_zones_source`), intensity distribution, Garmin load focus, illness risk, recent activities, next event, and `consistency_checks`.
- `Mainspring:get_training_goals` — weekly targets.
- `Mainspring:list_training_events` — goal race(s), target times, days to race.
- `Mainspring:get_suggested_workout` — Garmin Coach's workout for today, if the athlete is on a Garmin plan.
- `Garmin:get_scheduled_workouts` for today through the next 7 days — what's already on the calendar.
- `Mainspring:get_daily_metrics` for the last 7–10 days, and `Mainspring:get_logs` for the last 2–3 days (alcohol, illness notes, soreness).
- `Garmin:get_training_status` and `Garmin:get_race_predictions` (secondary context only).
- Location: raw start coordinates from the most recent `Garmin:get_activities` entry (fall back to the home location given in the routine's prompt). Weather via `AccuWeather:widgets-search-claude` → `AccuWeather:widgets-current-claude` + `AccuWeather:widgets-daily-claude`, plus `AccuWeather:widgets-gov-alert-map-claude`. Temperatures in Celsius.

### 2. Decide today's session

**Phase.** From `list_training_events`:
- No event within ~16 weeks → **off-season**: mostly easy aerobic running, strides 1–2×/week, hill sprints or short hills 1×/week, strength 2×/week (loaded, plus a short plyometric block). No marathon-pace work.
- 16 to ~3 weeks out → **build**: 1–2 quality sessions per week (threshold or tempo; intervals early in the block; marathon-pace blocks inside long runs in the last 8–10 weeks), one long run per week progressing toward 28–32 km, strength 1–2×/week.
- Under ~3 weeks → **taper**: volume down 40–60%, keep short race-pace or tempo segments, no new hard sessions to "fix" a Garmin load shortage.

**Rules (override everything below):**
1. Read `consistency_checks` first. If a check is a `"conflict"`, do not act on the disputed signal alone; follow the guide's resolution (e.g. never cut intensity on `grey_zone_flag` while Garmin reports a high-aerobic shortage). Mention the conflict in the email's second line.
2. **Protect quality sessions.** Being under the weekly km target is never a reason to replace a quality session with an easy run. Only a real red flag moves one: illness_risk **red**, HRV below −1 SD for several days, RPE 8+ yesterday, active soreness in the working muscles, or sleep well short of need. Then move it to the next suitable day; don't drop it.
3. Never two quality days in a row. A long run near marathon pace counts as quality. Strength goes on easy days, never the day before a quality session.
4. If the athlete is following a Garmin Coach plan and Garmin's session fits these rules, prefer it (say so) rather than inventing a different one.

**Then weigh:** red flags (as defined above; a yellow illness level alone is not one) → readiness and form_pct per the guide → weekly volume (add gradually, ~10% per week, but see rule 2) → weather (shift timing for storms, ice, extreme heat or active alerts; don't skip unless recovery also argues for rest) → race proximity.

**Pick exactly one session:**
- Easy run (distance or time, HR ceiling = top of today's zone 2 from `hr_zones`)
- Easy run + strides (e.g. 6×20 s relaxed-fast, full recovery)
- Long run (optionally with a marathon-pace block in the second half during a build)
- Threshold / tempo (e.g. 3×10 min or 2×15 min at threshold, or 20–40 min at tempo)
- Intervals (e.g. 5×3 min near 5 km effort) — early build or off-season only
- Strength (loaded or plyometric emphasis per phase)
- Rest

### 3. Put the workout on Garmin
- Check today's calendar first; never delete or overwrite an existing entry without checking.
- If Garmin's own scheduled workout is the pick, leave it as is.
- Otherwise build it with `Garmin:upload_workout` (warm-up / main set / cool-down steps, with HR-zone or pace targets) and `Garmin:schedule_workout` for today. **Do not use `create_z2_walk_workout` or `create_walk_run_workout` for runs** — they produce walk-run sessions. Use them only if a walk is explicitly the session.
- Strength: reuse a matching workout from `Garmin:get_workouts` (most recently used first), else `Garmin:create_strength_workout`.
- Rest day: schedule nothing.

### 4. Send the email
Send directly with `Gmail:send_message` (not a draft) to **the recipient**, subject `Daily Workout`.

First line, exactly one of:
- `Target workout: [session, with zone or pace targets, e.g. "10 km easy, Z2 111–126 bpm" or "Threshold 3×10 min @ ~4:35/km, 2 min jog"]` — use today's actual zones from `hr_zones` and say if `hr_zones_source` isn't "garmin"
- `Target workout: Strength session (~30 min) — [loaded / plyometric emphasis]`
- `Take a rest day`

Optional short parenthetical on the same line if something drove the call. Optional second line for rationale: phase, quality sessions done/remaining this week, weather, any consistency conflict, recovery context. No greetings or sign-offs.

### 5. Constraints
- If Mainspring or Garmin data is unavailable or incomplete (but sleep data is present), default to a conservative easy run (30–40 min, zone 2) or rest, never a quality session, and say what was missing.
- Send directly; never leave a draft.
- If sending fails, call `Mainspring:release_daily_recommendation(date=<same date>)` before exiting.
