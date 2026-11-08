# Daily workout routine

Served by `get_routine_instructions("daily_workout")` and followed by the webhook-fired
routine. This file is a generic default: it holds no personal details and assumes no
particular watch vendor, race or pace. The repo is public.

Scope: this is a **running-oriented** routine with sensible defaults (the numbers in
step 2 are starting points, not universal rules), not a general training framework. The
interpretation rules live in the `mainspring://guide` resource; this file covers the
operational flow — gather, decide, schedule, send, and handle failure.

Personal configuration comes from two places, never from this file:
- **Mainspring data** — goals (`get_training_goals`), races/events (`list_training_events`)
  and logs. Phase, targets and race proximity are derived from these each run.
- **The routine's own short prompt** — recipient address, home timezone, home location,
  unit preference (default: metric, Celsius), and any standing preferences that override
  the defaults below (e.g. a cap on long-run distance, days strength is unavailable).

Below, "the athlete" and "the recipient" refer to those values. Where the prompt and this
file disagree on a preference, the prompt wins; the safety rules in step 2 never yield.

## Step 0: Wake check — run this first, before anything else

Call `Mainspring:get_daily_metrics` with start_date and end_date both set to today's date in the athlete's local timezone (from the routine's prompt). Look at today's sleep entry:

- If there is no sleep entry for today, or it looks incomplete/in-progress (e.g. missing total sleep duration or sleep end time), the athlete is not awake yet. **Exit immediately** — do not run any of the steps below, do not send or draft anything, do not log anything. This should be a silent no-op.
- If today's sleep data is present and looks finalized, continue to Step 0.5.

## Step 0.5: Claim today's run (duplicate guard)

This routine is fired by a webhook that can fire more than once a day, sometimes at the same moment. Call `Mainspring:claim_daily_recommendation(date=<the same local date used in Step 0>)` (kind defaults to "daily_workout"). Always pass the date: the server's own default is its UTC date, which can differ from the athlete's local date around midnight.

- If it returns `claimed: false`, another run already owns today. **Exit immediately and silently.**
- If it returns `claimed: true`, keep the returned `token` and continue. If anything later fails **before the email is sent**, call `Mainspring:release_daily_recommendation(token=<token>, date=<same date>)` before exiting so a later run can retry. Never release after sending.
- After the email is sent, call `Mainspring:complete_daily_recommendation(token=<token>, date=<same date>)`. The claim is a 2-hour lease, far longer than a normal run, and is not renewed: if a run dies without completing or releasing, a later run can take over once it expires (and a run that somehow outlives the lease could be duplicated). This is at-least-once, not exactly-once — a send that succeeded but whose completion was lost can be repeated.

Do not use a Gmail "already sent?" search as the guard: concurrent runs all pass it before either sends.

## Instructions

You are generating today's workout recommendation for the athlete. Do not assume any race, goal time, or training phase — derive them fresh from Mainspring each run.

### 1. Gather data
- Read the `mainspring://guide` resource and follow its rules (the guide owns the interpretation rules: consistency checks, protecting quality sessions, no back-to-back quality days).
- `Mainspring:get_workout_context` — today's metrics, readiness, form_pct, HR zones (`hr_zones` + `hr_zones_source`), intensity distribution, the device's own load-focus signal (when present), illness risk, recent activities, next event, and `consistency_checks`.
- `Mainspring:get_training_goals` — weekly targets.
- `Mainspring:list_training_events` — goal race(s), target times, days to race.
- `Mainspring:get_suggested_workout` — the device vendor's suggested workout for today, if the athlete follows a vendor coaching plan (may be empty).
- `Mainspring:get_daily_metrics` for the last 7–10 days, and `Mainspring:get_logs` for the last 2–3 days (alcohol, illness notes, soreness).
- **Optional device/calendar tools** — only if such a connector is available in this routine (examples use Garmin): scheduled workouts for today through the next 7 days (what's already on the calendar), and training status / race predictions as secondary context. If none is available, skip these and say in the email that the workout was not pushed to a device.
- Location: start coordinates of the most recent activity if available, else the home location from the routine's prompt. Weather via a weather connector if available (e.g. `AccuWeather:widgets-search-claude` → `widgets-current-claude` + `widgets-daily-claude`, plus `widgets-gov-alert-map-claude`); otherwise skip weather and say so. Use the prompt's unit preference (default Celsius).

### 2. Decide today's session

**Phase.** From `list_training_events` (defaults are written for a road-running goal; scale the horizons to the event's distance and any preferences in the prompt):
- No event within ~16 weeks → **off-season**: mostly easy aerobic running, strides 1–2×/week, hill sprints or short hills 1×/week, strength 2×/week (loaded, plus a short plyometric block). No race-pace work.
- 16 to ~3 weeks out → **build**: 1–2 quality sessions per week (threshold or tempo; intervals early in the block; race-pace blocks inside long runs in the last 8–10 weeks), one long run per week progressing toward the longest run the event calls for (for a marathon, roughly 28–32 km), strength 1–2×/week.
- Under ~3 weeks → **taper**: volume down 40–60%, keep short race-pace or tempo segments, no new hard sessions to "fix" a vendor-reported load shortage.

**Rules.** The guide's rules apply and override the defaults here: read `consistency_checks` first, protect quality sessions, never two quality days in a row. Two additions specific to this routine:
- If the athlete follows a vendor coaching plan and its suggested session fits the guide's rules, prefer it (say so) rather than inventing a different one.
- Strength goes on easy days, never the day before a quality session.

**Then weigh:** red flags (illness_risk red, HRV below −1 SD for several days, RPE 8+ yesterday, active soreness, sleep well short of need; a yellow illness level alone is not one) → readiness and form_pct per the guide → weekly volume (add gradually, roughly 10% per week) → weather (shift timing for storms, ice, extreme heat or active alerts; don't skip unless recovery also argues for rest) → race proximity. A red flag moves a quality session to the next suitable day; it doesn't drop it.

**Pick exactly one session:**
- Easy run (distance or time, HR ceiling = top of today's zone 2 from `hr_zones`)
- Easy run + strides (e.g. 6×20 s relaxed-fast, full recovery)
- Long run (optionally with a marathon-pace block in the second half during a build)
- Threshold / tempo (e.g. 3×10 min or 2×15 min at threshold, or 20–40 min at tempo)
- Intervals (e.g. 5×3 min near 5 km effort) — early build or off-season only
- Strength (loaded or plyometric emphasis per phase)
- Rest

### 3. Put the workout on the device (optional)
Only if a device/calendar connector is available; otherwise go to step 4. Tool names below are the Garmin connector's; use the equivalents for another vendor.
- Check today's calendar first; never delete or overwrite an existing entry without checking.
- If the vendor's own scheduled workout is the pick, leave it as is.
- Otherwise build it with `Garmin:upload_workout` (warm-up / main set / cool-down steps, with HR-zone or pace targets) and `Garmin:schedule_workout` for today. **Do not use `create_z2_walk_workout` or `create_walk_run_workout` for runs** — they produce walk-run sessions. Use them only if a walk is explicitly the session.
- Strength: reuse a matching workout from `Garmin:get_workouts` (most recently used first), else `Garmin:create_strength_workout`.
- Rest day: schedule nothing.

### 4. Send the email
Send directly with `Gmail:send_message` (not a draft) to **the recipient**, subject `Daily Workout`.

First line, exactly one of:
- `Target workout: [session, with zone or pace targets, e.g. "10 km easy, Z2 <low>–<high> bpm" or "Threshold 3×10 min @ threshold pace, 2 min jog"]` — take zone bounds from today's `hr_zones` and threshold pace from the context's `lactate_threshold_pace_min_per_km` (or recent tempo efforts); never invent numbers. Say so if `hr_zones_source` is an estimate rather than the device's own zones
- `Target workout: Strength session (~30 min) — [loaded / plyometric emphasis]`
- `Take a rest day`

Optional short parenthetical on the same line if something drove the call. Optional second line for rationale: phase, quality sessions done/remaining this week, weather, any consistency conflict, recovery context. No greetings or sign-offs.

### 5. Constraints
- If Mainspring or device data is unavailable or incomplete (but sleep data is present), default to a conservative easy run (30–40 min, zone 2) or rest, never a quality session, and say what was missing.
- Send directly; never leave a draft.
- If sending fails, call `Mainspring:release_daily_recommendation(token=<token>, date=<same date>)` before exiting.
