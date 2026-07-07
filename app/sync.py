"""Per-day Square pull + override-preserving merge + nightly-sync helpers.

Provenance is DERIVED, never stored: a field's source is
  - "manual"   if the day has no pull or the pull didn't produce the field
  - "blocked"  if the latest pull flagged it (unmapped category/staff)
  - "square"   if the current input equals the pulled value
  - "override" if it differs (manager edited after the pull)
so re-pulls are idempotent and reverting an override is just setting the
input back to the Square value. Overrides survive re-pulls: a field whose
current value differs from the *previous* pull's value is manager-touched
and is left alone."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from engine import business_day_bounds

from . import settings_store
from .db import audit, utcnow
from .square import SquareClient
from .square_extract import (
    build_catalog_lookup,
    extract_auto_gratuity,
    extract_credit_tips,
    extract_food_sales,
    extract_lf_timecards,
    extract_server_tips,
    extract_timecards,
)

SQUARE_FIELDS = ("food_sales_cents", "credit_tips_cents", "auto_gratuity_cents",
                 "cash_tips_cents", "boh_worked", "foh_hours")
LF_SQUARE_FIELDS = ("server_tips", "server_cash_tips", "auto_gratuity_cents",
                    "hours", "unattributed_tips_cents")
SQUARE_FIELDS_BY_MODEL = {"POOL_HOURS": SQUARE_FIELDS,
                          "PERCENT_TIPOUT": LF_SQUARE_FIELDS}


def blocked_fields(square: dict | None) -> set[str]:
    if not square:
        return set()
    out: set[str] = set()
    for issue in square.get("issues", []):
        out.update(issue.get("blocks", []))
    return out


def merge_pull_into_inputs(old_inputs: dict, old_square: dict | None,
                           new_square: dict,
                           fields: tuple = SQUARE_FIELDS) -> dict:
    """Apply pulled values to day inputs, preserving manager overrides
    (fields whose current value differs from the previous pull's value)."""
    inputs = dict(old_inputs)
    old_values = (old_square or {}).get("values", {})
    blocked = blocked_fields(new_square)
    for field in fields:
        if field in blocked or field not in new_square["values"]:
            continue
        if field in old_values and inputs.get(field) != old_values[field]:
            continue  # manager override — keep it
        inputs[field] = new_square["values"][field]
    return inputs


def pull_day(conn: sqlite3.Connection, client: SquareClient, venue: sqlite3.Row,
             business_day: date, user_id: int | None) -> dict:
    """Fetch one business day from Square, extract, and return the square
    record to store on the day row. Pure fetch+extract — the caller merges
    and persists."""
    settings = settings_store.all_settings(conn, venue["id"])
    start, end = business_day_bounds(
        business_day, ZoneInfo(venue["timezone"]),
        cutoff_minutes=settings["day_cutoff_minutes"],
    )
    begin_iso, end_iso = start.isoformat(), end.isoformat()

    payments = client.list_payments(begin_iso, end_iso)
    orders = client.search_orders(begin_iso, end_iso)
    timecards = client.search_timecards(begin_iso, end_iso)

    emp_rows_all = conn.execute(
        "SELECT l.team_member_id AS tmid, e.* FROM square_link l"
        " JOIN employee e ON e.id = l.employee_id"
        " WHERE l.venue_id = ?",
        (venue["id"],),
    ).fetchall()
    # several Square accounts may map to the same person; extractors
    # aggregate by employee id, so hours/tips just sum across accounts
    emp_by_tmid = {r["tmid"]: dict(r) for r in emp_rows_all}

    if venue["tip_model"] == "PERCENT_TIPOUT":
        return _pull_values_lf(payments, orders, timecards, emp_by_tmid,
                               settings, user_id)

    var_ids = sorted({
        li["catalog_object_id"]
        for o in orders for li in o.get("line_items", [])
        if li.get("catalog_object_id")
    })
    catalog_lookup = build_catalog_lookup(
        client.batch_retrieve_catalog(var_ids) if var_ids else {}
    )

    food = extract_food_sales(orders, catalog_lookup, settings["category_map"])
    tips = extract_credit_tips(payments)
    grat = extract_auto_gratuity(orders, settings["gratuity_service_charge"])
    labor = extract_timecards(
        timecards, emp_by_tmid, business_day,
        settings_store.windows_by_weekday(settings), venue["timezone"],
        settings_store.rounding_increment(settings),
    )

    issues = food["issues"] + labor["issues"]
    labor_blocked = any(i["code"] == "unmapped_team_member" for i in labor["issues"])
    values = {
        "food_sales_cents": food["food_sales_cents"],
        "credit_tips_cents": tips["credit_tips_cents"],
        "auto_gratuity_cents": grat["auto_gratuity_cents"],
    }
    if not labor_blocked:
        values.update({
            "cash_tips_cents": labor["cash_tips_cents"],
            "boh_worked": labor["boh_worked"],
            "foh_hours": labor["foh_hours"],
        })

    return {
        "pulled_at": utcnow(),
        "pulled_by": user_id,
        "values": values,
        "issues": issues,
        "raw": {
            "food_lines": food["lines"],
            "payments": tips["payments"],
            "service_charges": grat["charges"],
            "timecards": labor["timecards"],
            "counts": {"payments": len(payments), "orders": len(orders),
                       "timecards": len(timecards)},
        },
    }


def _pull_values_lf(payments, orders, timecards, emp_by_tmid,
                    settings, user_id) -> dict:
    """PERCENT_TIPOUT pull: per-server tip attribution + full-shift hours.
    No food-sales/category mapping (not part of the LF model, M5 §4) and no
    tippable-window clipping (M5 §3)."""
    tips = extract_server_tips(payments, emp_by_tmid)
    labor = extract_lf_timecards(timecards, emp_by_tmid)
    grat = extract_auto_gratuity(orders, settings["gratuity_service_charge"])

    issues = tips["issues"] + labor["issues"]
    # blocking issues from either source suppress all labor/tip fields
    blocked = any(i["severity"] == "blocking" for i in issues)
    values = {"auto_gratuity_cents": grat["auto_gratuity_cents"]}
    if not blocked:
        values.update({
            "server_tips": tips["server_tips"],
            "server_cash_tips": labor["server_cash_tips"],
            "hours": labor["hours"],
            "unattributed_tips_cents": tips["unattributed_tips_cents"],
        })

    return {
        "pulled_at": utcnow(),
        "pulled_by": user_id,
        "values": values,
        "issues": issues,
        "raw": {
            "payments": tips["payments"],
            "service_charges": grat["charges"],
            "timecards": labor["timecards"],
            "counts": {"payments": len(payments), "orders": len(orders),
                       "timecards": len(timecards)},
        },
    }


# ---------- nightly sync ----------

def seconds_until_hour(now: datetime, hour: int) -> float:
    """Seconds from `now` (tz-aware, venue tz) until the next occurrence of
    `hour`:00 local."""
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def nightly_target_day(now: datetime) -> date:
    """The nightly job syncs the *prior* business day."""
    return now.date() - timedelta(days=1)


def should_auto_sync(day_row: sqlite3.Row | None) -> bool:
    """Skip days a human already finalized; drafts and untouched days sync."""
    return day_row is None or day_row["status"] != "finalized"
