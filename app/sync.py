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
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from engine import business_day_bounds

from . import settings_store
from .db import audit, utcnow
from .square import SquareClient
from .square_extract import (
    _amount,
    extract_timecards_poq,
    build_catalog_lookup,
    extract_auto_gratuity,
    _iso,
    extract_credit_tips,
    extract_event_money,
    extract_food_sales,
    extract_lf_timecards,
    extract_server_tips,
    extract_timecards,
)

SQUARE_FIELDS = ("food_sales_cents", "credit_tips_cents", "auto_gratuity_cents",
                 "cash_tips_cents", "boh_worked", "foh_hours")
LF_SQUARE_FIELDS = ("server_tips", "server_cash_tips", "auto_gratuity_cents",
                    "hours", "unattributed_tips_cents")
POQ_SQUARE_FIELDS = ("credit_tips_cents", "cash_tips_cents",
                     "auto_gratuity_cents", "shifts", "net_sales_cents",
                     "event_service_charge_cents", "event_tips_cents",
                     "event_card_cents", "event_start", "event_end",
                     "event_bartender_employee_id", "event_bartender_hours")
SQUARE_FIELDS_BY_MODEL = {"POOL_HOURS": SQUARE_FIELDS,
                          "PERCENT_TIPOUT": LF_SQUARE_FIELDS,
                          "POINTS_HOURS": POQ_SQUARE_FIELDS}


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
    if venue["tip_model"] == "POINTS_HOURS":
        return _pull_values_poq(payments, orders, timecards, emp_by_tmid,
                                settings, venue, user_id)

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
    grat = extract_auto_gratuity(orders, settings["gratuity_service_charge"], payments)
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
    grat = extract_auto_gratuity(orders, settings["gratuity_service_charge"], payments)

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


def _event_bartender(shifts: list[dict], window: dict) -> tuple[dict | None, list[dict]]:
    """Who tended bar during the event, and for how many of their hours.

    Poquitos has no Event Bartender job, so the bartender on duty covers a
    private event on an ordinary Bartender clock-in (owner 2026-08-28). Any
    bartender whose shift overlaps the event window is a candidate; when
    exactly one does, they are drafted automatically. When more than one does,
    nobody is drafted and the manager is asked to say which — the app must not
    guess who was behind the event bar.

    The hours returned are the overlap only. The rest of the shift stays in
    the daily pool, so the bartender earns from both.
    """
    start, end = _iso(window["start_at"]), _iso(window["end_at"])
    candidates = []
    for sh in shifts:
        if sh.get("role") != "BARTENDER" or not sh.get("start_at") or not sh.get("end_at"):
            continue
        overlap = (min(_iso(sh["end_at"]), end).timestamp()
                   - max(_iso(sh["start_at"]), start).timestamp())
        if overlap <= 0:
            continue
        hours = float((Decimal(round(overlap)) / 3600).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP))
        # never credit more event hours than the shift actually holds
        hours = min(hours, float(sh["hours"]))
        if hours <= 0:
            continue
        candidates.append({"employee_id": sh["employee_id"],
                           "name": sh.get("name") or str(sh["employee_id"]),
                           "hours": hours})
    candidates.sort(key=lambda c: c["name"])
    return (candidates[0] if len(candidates) == 1 else None), candidates


def _pull_values_poq(payments, orders, timecards, emp_by_tmid, settings,
                     venue, user_id) -> dict:
    """POINTS_HOURS (Poquitos): card tips, auto-gratuity, and one shift per
    timecard carrying the Square job chosen at clock-in. No food sales — the
    pool is a straight 80/20 of tips, not a % of food."""
    # A private event is whatever the shared "Event Host" pin rang (owner
    # 2026-08-28). Pull it out FIRST: its 20% service charge is an
    # AUTO_GRATUITY like any other, so without this the event's money would be
    # distributed to the daily pool and the event's own staff would get
    # nothing — which is exactly what happened to 2026-08-17.
    event_tmid = str(settings.get("poq_event_logon_tmid") or "")
    event = extract_event_money(orders, payments, event_tmid, venue["timezone"],
                                settings["gratuity_service_charge"])

    tips = extract_credit_tips(payments, exclude_order_ids=event["order_ids"])
    grat = extract_auto_gratuity(orders, settings["gratuity_service_charge"],
                                 payments, exclude_order_ids=event["order_ids"])
    # Net sales (ex tax, tip and service charge) — not part of the payout math,
    # but it is the denominator for the period's tip rate. Reproduces Square's
    # own "Total Sales" figure exactly.
    net_sales = sum(
        _amount((o.get("net_amounts") or {}).get("total_money"))
        - _amount((o.get("net_amounts") or {}).get("tax_money"))
        - _amount((o.get("net_amounts") or {}).get("tip_money"))
        - _amount((o.get("net_amounts") or {}).get("service_charge_money"))
        for o in orders
    )
    labor = extract_timecards_poq(
        timecards, emp_by_tmid, venue["timezone"],
        # unused by this extractor: Poquitos keeps hours as Square reports
        # them (2dp, nearest) rather than rounding to an increment
        Decimal("0"),
        settings_store.poq_job_roles(settings),
        ignore_tmids=[event_tmid] if event_tmid else [],
    )

    issues = labor["issues"]
    blocked = {i["code"] for i in issues if i["severity"] == "blocking"}
    values = {
        "credit_tips_cents": tips["credit_tips_cents"],
        "auto_gratuity_cents": grat["auto_gratuity_cents"],
        "net_sales_cents": net_sales,
    }
    if not blocked:
        values["cash_tips_cents"] = labor["cash_tips_cents"]
        values["shifts"] = [
            {"employee_id": s["employee_id"], "role": s["role"], "hours": s["hours"]}
            for s in labor["shifts"]
        ]

    if event["window"]:
        values["event_service_charge_cents"] = event["event_service_charge_cents"]
        values["event_tips_cents"] = event["event_tips_cents"]
        values["event_card_cents"] = event["card_cents"]
        values["event_start"] = event["window"]["start_at"]
        values["event_end"] = event["window"]["end_at"]
        if not blocked:
            picked, candidates = _event_bartender(labor["shifts"], event["window"])
            values["event_bartender_employee_id"] = picked["employee_id"] if picked else None
            values["event_bartender_hours"] = picked["hours"] if picked else 0.0
            if len(candidates) > 1:
                issues.append({
                    "severity": "warning", "code": "pick_event_bartender",
                    "detail": [f"{c['name']} ({c['hours']:.2f} h on the event)"
                               for c in candidates],
                    "blocks": [],
                })
            elif not candidates:
                issues.append({"severity": "warning", "code": "no_event_bartender",
                               "detail": [], "blocks": []})
        if event["other_charges"]:
            # by policy the 3% admin fee goes to the organising manager and
            # never touches the staff pool, so it is held out — but say so,
            # rather than letting money vanish from the ticket silently
            issues.append({
                "severity": "warning", "code": "event_non_gratuity_charge",
                "detail": [f"{c['name']} {'' if c['percentage'] is None else c['percentage'] + '% '}"
                           f"${c['cents'] / 100:.2f}" for c in event["other_charges"]],
                "blocks": [],
            })

    return {
        "pulled_at": utcnow(),
        "pulled_by": user_id,
        "values": values,
        "issues": issues,
        "raw": {
            "payments": tips["payments"],
            "service_charges": grat["charges"],
            "event_orders": event["orders"],
            "event_other_charges": event["other_charges"],
            "shifts": labor["shifts"],
            "counts": {"payments": len(payments), "orders": len(orders),
                       "timecards": len(timecards)},
        },
    }


def refresh_labor_shifts(conn, client, venue, business_day: date) -> list[dict]:
    """Re-fetch just the day's timecards and return the extracted shifts.

    Used to backfill clock times and hourly rates onto days that were
    finalized before those were stored. Deliberately narrower than
    `pull_day`: it touches no money, so the caller can write the result onto
    a FINALIZED day without any risk of moving a locked payout.
    """
    settings = settings_store.all_settings(conn, venue["id"])
    start, end = business_day_bounds(
        business_day, ZoneInfo(venue["timezone"]),
        cutoff_minutes=settings["day_cutoff_minutes"],
    )
    timecards = client.search_timecards(start.isoformat(), end.isoformat())
    emp_rows = conn.execute(
        "SELECT l.team_member_id AS tmid, e.* FROM square_link l"
        " JOIN employee e ON e.id = l.employee_id"
        " WHERE l.venue_id = ?",
        (venue["id"],),
    ).fetchall()
    labor = extract_timecards_poq(
        timecards, {r["tmid"]: dict(r) for r in emp_rows}, venue["timezone"],
        Decimal("0"), settings_store.poq_job_roles(settings),
    )
    return labor["shifts"]
