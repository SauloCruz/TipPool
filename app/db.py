"""SQLite access. Per-request connections + WAL: safe for the 1-3 concurrent
users this app will ever see, no ORM to keep it boring."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SCHEMA_VERSION = 7

# Incremental migrations applied in order after the base schema.
MIGRATIONS: dict[int, str] = {
    2: """
    ALTER TABLE employee ADD COLUMN square_team_member_id TEXT;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_emp_sq_tmid
        ON employee (square_team_member_id)
        WHERE square_team_member_id IS NOT NULL;
    -- raw Square pull (values + extracts + issues) for the day, JSON
    ALTER TABLE day ADD COLUMN square_json TEXT;
    CREATE TABLE IF NOT EXISTS setting (
        venue_id   INTEGER NOT NULL REFERENCES venue(id),
        key        TEXT NOT NULL,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by INTEGER REFERENCES user(id),
        PRIMARY KEY (venue_id, key)
    );
    """,
    # M5: multi-venue + PERCENT_TIPOUT (La Fontana). Additive only —
    # Tavern Law rows are untouched.
    3: """
    ALTER TABLE venue ADD COLUMN slug TEXT;
    ALTER TABLE venue ADD COLUMN tip_model TEXT NOT NULL DEFAULT 'POOL_HOURS';

    -- widen employee.pool_role CHECK for La Fontana roles (table rebuild;
    -- nothing holds a foreign key to employee, ids are preserved)
    CREATE TABLE employee_v3 (
        id                    INTEGER PRIMARY KEY,
        venue_id              INTEGER NOT NULL REFERENCES venue(id),
        display_name          TEXT NOT NULL,
        pool_role             TEXT NOT NULL CHECK (pool_role IN
            ('FOH', 'BOH', 'EXCLUDED', 'SERVER', 'BUSSER', 'HOST')),
        active                INTEGER NOT NULL DEFAULT 1,
        created_at            TEXT NOT NULL,
        square_team_member_id TEXT,
        UNIQUE (venue_id, display_name)
    );
    INSERT INTO employee_v3 (id, venue_id, display_name, pool_role, active,
                             created_at, square_team_member_id)
        SELECT id, venue_id, display_name, pool_role, active, created_at,
               square_team_member_id FROM employee;
    DROP TABLE employee;
    ALTER TABLE employee_v3 RENAME TO employee;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_emp_sq_tmid
        ON employee (square_team_member_id)
        WHERE square_team_member_id IS NOT NULL;

    -- RBAC deferred (owner ruling): table exists now, unenforced.
    CREATE TABLE IF NOT EXISTS user_venue_access (
        user_id  INTEGER NOT NULL REFERENCES user(id),
        venue_id INTEGER NOT NULL REFERENCES venue(id),
        role     TEXT NOT NULL CHECK (role IN ('manager', 'admin')),
        PRIMARY KEY (user_id, venue_id)
    );
    """,
    # M5.1: La Fontana pays tips in cash weekly — per-employee round-up
    # increment. NULL = venue default (nearest $1), 0 = no rounding,
    # else round up to this many cents (e.g. 500 = nearest $5).
    4: """
    ALTER TABLE employee ADD COLUMN round_up_cents INTEGER;
    """,
    # M5.2: one person may have SEVERAL Square team-member accounts (e.g.
    # one per job). Many-to-one link table replaces the single
    # employee.square_team_member_id column (kept but no longer written).
    5: """
    CREATE TABLE IF NOT EXISTS square_link (
        venue_id       INTEGER NOT NULL REFERENCES venue(id),
        team_member_id TEXT NOT NULL,
        employee_id    INTEGER NOT NULL REFERENCES employee(id),
        PRIMARY KEY (venue_id, team_member_id)
    );
    INSERT OR IGNORE INTO square_link (venue_id, team_member_id, employee_id)
        SELECT venue_id, square_team_member_id, id FROM employee
        WHERE square_team_member_id IS NOT NULL;
    """,
    # M5.3: salaried kitchen staff (LF chef) never clock in but always share
    # the monthly BOH pool — flag pre-selects them on the export roster.
    6: """
    ALTER TABLE employee ADD COLUMN always_in_boh_pool INTEGER NOT NULL DEFAULT 0;
    """,
    # M6: real venue RBAC. Existing first admin becomes Super Admin so the
    # owner is not locked out when access enforcement turns on.
    7: """
    ALTER TABLE user ADD COLUMN super_admin INTEGER NOT NULL DEFAULT 0;
    UPDATE user SET super_admin = 1
    WHERE id = (
        SELECT id FROM user WHERE role = 'admin' ORDER BY id LIMIT 1
    )
    AND NOT EXISTS (SELECT 1 FROM user WHERE super_admin = 1);
    INSERT OR IGNORE INTO user_venue_access (user_id, venue_id, role)
        SELECT id, venue_id, role FROM user;
    """,
}

# slug/venue seeding that migrations can't express declaratively
VENUE_SEEDS = [
    # (slug, name, tip_model) — first entry adopts the existing venue row
    ("tavern-law", "Tavern Law", "POOL_HOURS"),
    ("la-fontana", "La Fontana Siciliana", "PERCENT_TIPOUT"),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI may enter/use/close a request's
    # connection on different threadpool threads. Each connection still
    # serves exactly one request at a time, so this is safe.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: Path, venue_name: str, tz: str) -> None:
    conn = connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            conn.executescript(SCHEMA_PATH.read_text())
            version = 1
        for v in sorted(MIGRATIONS):
            if version < v:
                conn.executescript(MIGRATIONS[v])
                version = v
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        venue = conn.execute("SELECT id FROM venue LIMIT 1").fetchone()
        if venue is None:
            conn.execute(
                "INSERT INTO venue (name, timezone) VALUES (?, ?)", (venue_name, tz)
            )
        # M5 venue seeding: the original single venue adopts the first seed's
        # slug; later seeds are inserted if missing. Existing rows untouched.
        first_slug, _, first_model = VENUE_SEEDS[0]
        conn.execute(
            "UPDATE venue SET slug = ?, tip_model = ? WHERE slug IS NULL AND id ="
            " (SELECT MIN(id) FROM venue)",
            (first_slug, first_model),
        )
        for slug, name, tip_model in VENUE_SEEDS[1:]:
            exists = conn.execute(
                "SELECT 1 FROM venue WHERE slug = ?", (slug,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO venue (name, timezone, slug, tip_model)"
                    " VALUES (?, ?, ?, ?)",
                    (name, tz, slug, tip_model),
                )
        conn.commit()
    finally:
        conn.close()


def audit(
    conn: sqlite3.Connection,
    venue_id: int,
    user_id: int | None,
    action: str,
    entity: str,
    entity_id,
    detail_json: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (venue_id, user_id, ts, action, entity, entity_id, detail_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (venue_id, user_id, utcnow(), action, entity, str(entity_id), detail_json),
    )
