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
    # Owner ruling 2026-07-29: credit tippable hours in 0.05 steps, always
    # rounded UP ("to the next 5 or 0": 0.78 -> 0.80). Applies to Square-pulled
    # and hand-entered hours alike. Clock times themselves are never rounded;
    # window clipping still applies. Supersedes the 2026-07-05 0.01 ruling.
    "rounding_increment": "0.05",
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
    # POOL_HOURS (Tavern Law) — tip credit per hour for a host/door shift,
    # marked per day on the day screen (staff work dual roles, so this is not
    # a fixed per-person role). Owner ruling 2026-07-29: half credit, applied
    # to the tip pool AND the auto-gratuity pool. "1" disables the reduction.
    "tl_door_weight": "0.5",
    # ---- POINTS_HOURS (Poquitos, M6) ----
    # Role catalogue: points per hour and which pool the role feeds.
    # side FOH/BOH share the daily split; EVENT is event-pool only (and out
    # of the daily pool); EXCLUDED earns nothing anywhere. Owner rulings
    # 2026-08-03 and 2026-08-13 — see docs/M6-poquitos.md.
    "poq_roles": {
        "BARTENDER": {"points": "1.25", "side": "FOH"},
        "SHIFT_LEAD": {"points": "1.25", "side": "FOH"},
        "SERVER": {"points": "1", "side": "FOH"},
        "BARBACK": {"points": "0.5", "side": "FOH"},
        "BAR_PREP": {"points": "0.5", "side": "FOH"},
        "HOST": {"points": "0.5", "side": "FOH"},
        "BUSSER": {"points": "0.5", "side": "FOH"},
        "EXPEDITOR": {"points": "0.5", "side": "FOH"},
        "FOOD_RUNNER": {"points": "0.5", "side": "FOH"},
        "SOUS_CHEF": {"points": "1", "side": "BOH"},
        "LINE_COOK": {"points": "1", "side": "BOH"},
        "PREP_COOK": {"points": "1", "side": "BOH"},
        "DISHWASHER": {"points": "1", "side": "BOH"},
        "EVENT_SERVER": {"points": "1", "side": "EVENT"},
        "EVENT_BARTENDER": {"points": "1.25", "side": "EVENT"},
        "SHIFT_MANAGER": {"points": "0", "side": "EXCLUDED"},
        "KITCHEN_MANAGER": {"points": "0", "side": "EXCLUDED"},
        "OWNER": {"points": "0", "side": "EXCLUDED"},
        "JANITORIAL": {"points": "0", "side": "EXCLUDED"},
        "STAFF_TRAINER": {"points": "0", "side": "EXCLUDED"},
        "TRAINING_SHIFT": {"points": "0", "side": "EXCLUDED"},
    },
    # Square job title (exactly as Square spells it) -> role above. A title
    # seen on a timecard but missing here BLOCKS the day — never guess a rate.
    "poq_job_roles": {
        "Bartender": "BARTENDER",
        "Shift Lead": "SHIFT_LEAD",
        "Server": "SERVER",
        "Bar Prep": "BAR_PREP",
        "Busser": "BUSSER",
        "Host": "HOST",
        "Runner": "FOOD_RUNNER",
        "Line Cook": "LINE_COOK",
        "Prep Cook": "PREP_COOK",
        "Dishwasher": "DISHWASHER",
        "Event Server": "EVENT_SERVER",
        "Shift manager": "SHIFT_MANAGER",
        "Kitchen Manager": "KITCHEN_MANAGER",
        "Owner": "OWNER",
        "Janitorial": "JANITORIAL",
        "Staff Trainer": "STAFF_TRAINER",
        "Training Shift": "TRAINING_SHIFT",
    },
    # % of pooled tips to FOH; BOH takes the exact remainder.
    "poq_foh_pct": "80",
    # event support tip-out, per role group, of the event's FOH portion
    "poq_support_pct": "3",
    # Card processing fee withheld from CREDIT tips before pooling (owner
    # 2026-08-13). "0" = no deduction; set the venue's real processor rate in
    # Setup. Cash tips are never touched — no processor handles them.
    "poq_card_fee_pct": "0",
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


# ---- POINTS_HOURS (Poquitos) helpers: settings -> engine shapes ----

def poq_role_points(settings: dict) -> dict[str, Decimal]:
    return {r: Decimal(str(v["points"])) for r, v in settings["poq_roles"].items()}


def poq_role_side(settings: dict) -> dict[str, str]:
    return {r: v["side"] for r, v in settings["poq_roles"].items()}


def poq_job_roles(settings: dict) -> dict[str, str]:
    """Square job title (verbatim) -> role key."""
    return dict(settings["poq_job_roles"])
