-- Tavern Law Tip Pool — M2 schema.
-- venue_id everywhere per CLAUDE.md §8 (multi-venue is a non-goal but the
-- schema must not preclude it). Finalized days are never hard-deleted (§6:
-- retain >= 3 years) — there are no DELETE paths in the app.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS venue (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    timezone TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user (
    id            INTEGER PRIMARY KEY,
    venue_id      INTEGER NOT NULL REFERENCES venue(id),
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('manager', 'admin')),
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES user(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employee (
    id           INTEGER PRIMARY KEY,
    venue_id     INTEGER NOT NULL REFERENCES venue(id),
    display_name TEXT NOT NULL,
    -- EXCLUDED = manager/owner: hard-blocked from every pool (WA law, §6)
    pool_role    TEXT NOT NULL CHECK (pool_role IN ('FOH', 'BOH', 'EXCLUDED')),
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    UNIQUE (venue_id, display_name)
);

CREATE TABLE IF NOT EXISTS day (
    id           INTEGER PRIMARY KEY,
    venue_id     INTEGER NOT NULL REFERENCES venue(id),
    date         TEXT NOT NULL,              -- YYYY-MM-DD in venue timezone
    status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'finalized')),
    inputs_json  TEXT NOT NULL,              -- cents + employee-id rosters
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    updated_by   INTEGER REFERENCES user(id),
    finalized_at TEXT,
    finalized_by INTEGER REFERENCES user(id),
    UNIQUE (venue_id, date)
);

-- Immutable audit snapshots (§2 rule 6): finalizing writes inputs+outputs;
-- re-finalizing after a reopen writes the next version. Never updated/deleted.
CREATE TABLE IF NOT EXISTS day_snapshot (
    id             INTEGER PRIMARY KEY,
    day_id         INTEGER NOT NULL REFERENCES day(id),
    version        INTEGER NOT NULL,
    inputs_json    TEXT NOT NULL,
    outputs_json   TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    computed_at    TEXT NOT NULL,
    computed_by    INTEGER REFERENCES user(id),
    UNIQUE (day_id, version)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    venue_id    INTEGER NOT NULL REFERENCES venue(id),
    user_id     INTEGER REFERENCES user(id),
    ts          TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   TEXT,
    detail_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_day_venue_date ON day (venue_id, date);
CREATE INDEX IF NOT EXISTS idx_snapshot_day ON day_snapshot (day_id, version);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (venue_id, ts);
CREATE INDEX IF NOT EXISTS idx_session_expiry ON session (expires_at);
