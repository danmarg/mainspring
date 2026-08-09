# Strain-aware alertness curve — design plan

## Problem

Two questions the current dashboard doesn't cleanly separate:

1. **When today should I do the hard thing** (exercise, cognitively demanding work)?
2. **How much energy do I have today / how ready am I for hard training**?

Readiness (daily, morning snapshot from HRV/RHR/sleep/training load) already answers
#2 reasonably well. The alertness graph is meant to answer #1, but today it's driven
purely by a circadian/sleep-pressure model. It therefore misses a narrow but useful
within-day factor: recent aerobic exercise can temporarily change how available the
user feels for another demanding task.

Constraint: the user wears a Garmin only during exercise, and a Fitbit/Google Health
source the rest of the time. Any design has to be source-agnostic and handle two
devices covering different parts of the same day.

## Model

Keep readiness and alertness as two distinct signals. Alertness is a heuristic for
*within-day alertness adjusted for recent aerobic exertion*; it is not a direct measure
of total physiological recovery, overall energy, soreness, or Garmin-style Body Battery.
It has three base components plus separate short- and medium-term exercise adjustments:

```
alertness(t) = circadian_component(t)       # Process C, two-process model (Borbély)
             + homeostatic_sleep_component(t) # Process S, time-awake driven
             + acute_arousal(t)               # short post-exercise sympathetic effect
             − exercise_strain_debt(t)        # recent aerobic exertion, decays over time
```

`acute_arousal` and `exercise_strain_debt` are scoped to **exercise-effort load only**
for v1 (see "Scope decisions" below) — not a full Body Battery clone.

### Why two exercise effects?

- Exercise can briefly increase subjective alertness immediately after a session. Model
  it as a small positive effect that decays over roughly 1–3 hours.
- Hard exercise can also reduce perceived availability for a later demanding task.
  Model this separately as `exercise_strain_debt`.
- The debt's 4–8 hour default is deliberately an **intraday** adjustment, not a claim
  that recovery is complete in that period. Multi-day recovery remains the job of
  readiness and training-load signals.

### Exercise-effort load (v1 scope)

- TRIMP-style calc (Banister): HR-reserve-weighted intensity × duration, per logged
  workout, source-agnostic since it only needs an HR stream + resting/max HR.
- TRIMP is an estimate of *internal aerobic load*, not a complete measure of exercise
  stress. It can misrepresent intervals, resistance training, mixed-modal work, heat,
  caffeine, illness, and sparse or missing HR coverage.
- Do not invent a strain value when workout HR coverage is inadequate; omit it from the
  v1 adjustment. Decay it after workout end via time-constant τ (start at 6h; evaluate
  a 4–8h candidate range and refine through calibration below).

### Background/cognitive stress load — explicitly deferred

- Garmin's stress score (HRV-derived) is the right signal for this, but only exists
  when Garmin is worn — which for this user is almost never outside workouts, so
  there's no usable signal to fall back on anyway.
- A resting-HR-elevation proxy from Fitbit/Google was considered but is low-confidence
  (blunt signal, many confounds, doesn't isolate motion from stress). Not worth
  building until there's a reason to revisit.
- Practical effect: `strain_debt` will read as "physical exertion depletion," not
  "overall strain." Documented gap, not a bug.

## Data model changes

### Preserve existing typed intraday tables

The existing `intraday_hr`, `intraday_stress`, and `intraday_hrv` tables already
provide the timestamped, source-specific data this feature needs. Preserve them for
v1 rather than introducing `raw_intraday_metrics`; this avoids an unnecessary
migration and parallel representation. Steps remain a daily metric and are not an
input to TRIMP.

### Source selection: HR is the only metric that needs merge logic

- `stress`: Garmin-only when present, nothing to merge against.
- `heart_rate`: bucket to one-minute timestamps and use whichever source has non-null
  data. If both sources overlap, take one canonical bucket so samples are never
  double-counted.
  Devices aren't expected to overlap much given how the user actually wears them, so
  this isn't really "pick a preferred source" so much as "take what's there."
- Exercise windows (for scoping the TRIMP calc) reuse the existing activity-dedup
  window logic (`garmin_activities` start_time, ±15 min) rather than a new mechanism.

### Google Health / Fitbit source note

Confirm which API this actually is before building the importer — likely Google
Health Connect / Google Fit rather than the legacy Fitbit Web API (which Google has
been sunsetting). Field names and auth differ.

## Subjective ground truth: `log_energy` MCP tool

```
log_energy(ts?, level, note?)
```

- `level`: 1–5 ordinal (Likert-style, consistent with `log_rpe`/`log_soreness`).
  Chosen over binary because it's more expressive for day-to-day qualitative review;
  collapse to binary at modeling time instead of losing resolution at capture time.
- `ts`: optional, defaults to now.
- `note`: free text.
- Writes to `manual_logs`, same as existing log tools. No new table needed.

**Classification happens at read time, not write time.** Whether a given entry counts
as "morning readiness ground truth" or "intraday decay ground truth" is derived by
checking `ts` against the existing wake-time detection (already built for the morning
workout webhook gating) — not a stored flag. Keeps the tool dumb/reusable and the
classification logic in one place.

## Calibration / validation

Two different jobs, deliberately kept low-dimensional given expected data volume
(roughly one log/day → ~30 points/month).

### Morning energy → readiness validation

- Start with Spearman rank correlation (readiness score vs. reported energy) as an
  ongoing diagnostic — is the relationship monotonic at all.
- Optional: single-coefficient logistic regression, P(report "feels good") as a
  function of readiness score, as a calibration curve once there's enough data.
- Recalibrating the *inputs* to readiness (reweighting HRV/sleep/RHR) needs far more
  data (~10–20 outcome events per predictor) — explicitly deferred until there's a
  real backlog (months), not attempted with the initial volume.

### Intraday energy → decay time-constant (τ)

- Collapse 1–5 labels to binary for modeling: ≤2 = tired, ≥4 = good, 3 excluded as
  ambiguous.
- Profile likelihood over candidate τ values: for each τ, compute predicted
  `strain_debt(t)` at each logged timestamp, fit single-coefficient logistic
  regression of the binary label on predicted strain, take the τ with best
  log-likelihood/AUC.
- Run as a periodic offline job, not online/per-log learning — one noisy label can't
  responsibly move a model parameter. The completed hourly import chain checks a
  persisted, atomic weekly claim after normalization, so concurrent Garmin and Google
  Health imports cannot run it twice. It requires at least 20 intraday labels with at
  least five tired and five good ratings; an insufficient-data result is recorded and
  retried no sooner than a week later.
- Candidate τ values are 4.0–8.0h in 0.5h increments. The result (sample count,
  likelihood, AUC, and candidate scores) is stored in `model_calibration_runs` and
  exposed as a read-only suggestion through the admin/MCP status surfaces. It is never
  auto-applied to the live 6h dashboard parameter.

## Build order

1. Preserve the existing typed intraday tables; verify HR ingestion, one-minute source
   merging, and activity-window scoping.
2. Implement HR-derived TRIMP plus separate acute-arousal and strain-debt functions,
   wired into the alertness curve with a default debt τ of 6h.
3. Show the contribution as "exercise strain" in the existing alertness graph. Replace
   the peak triangle with a green horizontal bar spanning the contiguous range within
   85% of peak alertness.
4. `log_energy` MCP tool + wake-time-based classification at read time.
5. Profile-likelihood τ calibration runs at most weekly at the end of a successful
   import chain once the minimum `log_energy` volume is available; it stores and
   surfaces a suggestion rather than auto-tuning.
6. Once enough morning-log volume exists: readiness correlation diagnostic, and only
   later (months out) consider reweighting readiness inputs.

## Explicitly deferred / open

- Background/cognitive stress component of `exercise_strain_debt` — no usable signal
  given current wear habits; revisit if that changes.
- Strength, mixed-modal, and poorly sampled sessions — HR-only TRIMP cannot represent
  these reliably; revisit with session-RPE or modality-specific load.
- Multivariate readiness recalibration — needs more data than expected in the near
  term.
- Ordinal logistic regression for `log_energy` — binary collapse is good enough at
  expected sample sizes; revisit only if data volume grows substantially.
- Confirm actual Google Health API (Health Connect vs. legacy Fitbit Web API) before
  building the importer.
