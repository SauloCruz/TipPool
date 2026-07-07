"""Venue settings stored as JSON key-values: category mapping, gratuity
service-charge matcher, tippable windows per weekday, rounding increment.

Category groups: FOOD counts toward food_sales; everything else doesn't.
A Square category with group null is UNMAPPED and blocks the day's pull
(CLAUDE.md §3.1 — never silently guess)."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from engine import TippableWindow

from .db import audit, utcnow

CATEGORY_GROUPS = ("FOOD", "ALCOHOL", "NA_BEV", "RETAIL", "OTHER")

DEFAULTS = {
    # square_category_id -> {"name": str, "group": one of CATEGORY_GROUPS or None}
    "category_map": {},
    # matches an order service charge by exact catalog id, else name substring
    "gratuity_service_charge": {"catalog_object_id": None, "name_contains": "gratuity"},
    # weekday index (0=Mon .. 6=Sun) -> minutes after local midnight
    "tippable_windows": {
        str(wd): {"open_minutes": 17 * 60, "close_minutes": 24 * 60} for wd in range(7)
    },
    # Owner ruling 2026-07-05: hours are exact-to-the-minute like Square's
    # display (minutes/60, 2 decimals). Clock times are never rounded and
    # there is no quarter-hour rounding; window clipping still applies.
    "rounding_increment": "0.01",
    # When the business day ends, in minutes past midnight (120 = 2:00 AM).
    # Governs which calendar day a Square transaction/timecard belongs to —
    # late check settlements after midnight stay on the prior service day.
    # Independent of the tippable window, which still hard-stops at 24:00.
    "day_cutoff_minutes": 0,
    # warning codes hidden on the day screen (stored pulls keep everything)
    "muted_warnings": [],
    # cache of Square team members for the mapping UI (id, name, status)
    "square_team_cache": [],
    # PERCENT_TIPOUT (La Fontana) — of each server's OWN tips; must sum to 100
    "lf_percentages": {"server": "65", "busser": "20", "host": "10", "boh": "5"},
    # per-pool split: EVEN (owner default) or HOURS_PROPORTIONAL (ships OFF)
    "lf_pool_split_mode": {"busser": "EVEN", "host": "EVEN", "boh": "EVEN"},
    # no-host days re-split silently (low season runs thin); FLAG the day
    # only when fewer than this many bussers worked
    "lf_no_host_min_bussers": 3,
}


def get_raw(conn: sqlite3.Connection, venue_id: int, key: str, default=None):
    """Free-form venue records (e.g. per-month BOH rosters) that live in the
    setting table but are not part of DEFAULTS."""
    row = conn.execute(
        "SELECT value_json FROM setting WHERE venue_id = ? AND key = ?",
        (venue_id, key)).fetchone()
    return json.loads(row["value_json"]) if row else default


def put_raw(conn: sqlite3.Connection, venue_id: int, key: str, value,
            user_id: int | None) -> None:
    conn.execute(
        "INSERT INTO setting (venue_id, key, value_json, updated_at, updated_by)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (venue_id, key) DO UPDATE SET value_json = excluded.value_json,"
        " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (venue_id, key, json.dumps(value), utcnow(), user_id))
    audit(conn, venue_id, user_id, "setting_updated", "setting", key)


def get_setting(conn: sqlite3.Connection, venue_id: int, key: str):
    row = conn.execute(
        "SELECT value_json FROM setting WHERE venue_id = ? AND key = ?", (venue_id, key)
    ).fetchone()
    if row is None:
        return json.loads(json.dumps(DEFAULTS[key]))  # deep copy of default
    return json.loads(row["value_json"])


def put_setting(
    conn: sqlite3.Connection, venue_id: int, key: str, value, user_id: int | None
) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    conn.execute(
        "INSERT INTO setting (venue_id, key, value_json, updated_at, updated_by)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (venue_id, key) DO UPDATE SET value_json = excluded.value_json,"
        " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (venue_id, key, json.dumps(value), utcnow(), user_id),
    )
    audit(conn, venue_id, user_id, "setting_updated", "setting", key)


def all_settings(conn: sqlite3.Connection, venue_id: int) -> dict:
    return {key: get_setting(conn, venue_id, key) for key in DEFAULTS}


def windows_by_weekday(settings: dict) -> dict[int, TippableWindow]:
    return {
        int(wd): TippableWindow(
            open_minutes=w["open_minutes"], close_minutes=w["close_minutes"]
        )
        for wd, w in settings["tippable_windows"].items()
    }


def rounding_increment(settings: dict) -> Decimal:
    return Decimal(settings["rounding_increment"])
