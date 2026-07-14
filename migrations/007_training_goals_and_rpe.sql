-- steady-state weekly training targets (key-value, same pattern as source_config)
CREATE TABLE IF NOT EXISTS training_goals (
    metric     TEXT PRIMARY KEY,
    value      REAL NOT NULL,
    unit       TEXT,
    updated_at TEXT NOT NULL
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

-- RPE column on daily_metrics (resolved from manual_logs type='rpe')
ALTER TABLE daily_metrics ADD COLUMN rpe REAL;
