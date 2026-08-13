"""Bridge between stored day inputs (integer cents, employee IDs) and the
engine. Produces the outputs dict that goes into immutable snapshots.

Two tip models (M5): POOL_HOURS (Tavern Law, unchanged) and PERCENT_TIPOUT
(La Fontana). Dispatch happens in main.py on venue.tip_model; each model has
its own inputs shape and outputs shape. Outputs carry a "model" key —
absent means POOL_HOURS (pre-M5 snapshots)."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import engine
from engine import (
    ManagerInPoolError,
    Shift,
    compute_day,
    compute_day_percent_tipout,
    compute_day_points_hours,
    compute_event_points_hours,
)
from engine.clipping import DEFAULT_ROUNDING_INCREMENT
from engine.core import (
    DEFAULT_BOH_EVENT_FOOD_PCT,
    DEFAULT_BOH_FOOD_PCT,
    DEFAULT_DOOR_WEIGHT,
)


class DayValidationError(ValueError):
    """Input references employees that don't exist or sit in the wrong pool."""


EMPTY_INPUTS = {
    "food_sales_cents": 0,
    "event_food_sales_cents": 0,
    "credit_tips_cents": 0,
    "cash_tips_cents": 0,
    "event_tips_cents": 0,
    "auto_gratuity_cents": 0,
    "boh_worked": [],
    "foh_hours": {},
    # employee_ids working the host/door that day -> half tip credit per hour
    # (tl_door_weight). Absent in pre-2026-07-29 snapshots; treated as empty.
    "door_worked": [],
}

EMPTY_INPUTS_LF = {
    "server_tips": {},            # employee_id -> cents (card tips, attributed)
    "server_cash_tips": {},       # employee_id -> cents (declared at clock-out)
    "auto_gratuity_cents": 0,
    "hours": {},                  # employee_id -> worked hours (all roles)
    "unattributed_tips_cents": 0,          # card tips with no team member
    "unattributed_assignments": {},        # employee_id -> cents, manager-assigned
    "unattributed_house_cents": 0,         # manager-marked house / no-tip
}

EMPTY_INPUTS_BY_MODEL = {
    "POOL_HOURS": EMPTY_INPUTS,
    "PERCENT_TIPOUT": EMPTY_INPUTS_LF,
}


def _dollars(cents: int) -> Decimal:
    return Decimal(cents) / 100


def compute_outputs(inputs: dict, employees: dict[int, dict],
                    door_weight=DEFAULT_DOOR_WEIGHT,
                    hours_increment=DEFAULT_ROUNDING_INCREMENT) -> dict:
    """employees: id -> {display_name, pool_role}. Raises DayValidationError
    on unknown/wrong-pool employees; the EXCLUDED hard-block lives in the
    engine itself and is re-raised with a clear message.

    `door_weight` is the per-hour tip credit for staff marked on the day's
    `door_worked` list (venue setting tl_door_weight, default 1/2).
    `hours_increment` is reported so the UI can round typed hours the same way
    the server stores them; hours arriving here are already rounded."""
    boh_ids = [int(e) for e in inputs["boh_worked"]]
    foh_hours = {int(k): v for k, v in inputs["foh_hours"].items()}
    # door_worked is absent from snapshots predating the 2026-07-29 ruling
    door_ids = {int(e) for e in inputs.get("door_worked") or ()}

    problems = []
    for eid in boh_ids:
        emp = employees.get(eid)
        if emp is None:
            problems.append(f"unknown employee id {eid} in BOH roster")
        elif emp["pool_role"] == "FOH":
            problems.append(f"{emp['display_name']} is FOH, not BOH")
    for eid in foh_hours:
        emp = employees.get(eid)
        if emp is None:
            problems.append(f"unknown employee id {eid} in FOH hours")
        elif emp["pool_role"] == "BOH":
            problems.append(f"{emp['display_name']} is BOH, not FOH")
    for eid in sorted(door_ids):
        emp = employees.get(eid)
        if emp is None:
            problems.append(f"unknown employee id {eid} marked on the door")
        elif emp["pool_role"] == "BOH":
            problems.append(f"{emp['display_name']} is BOH and cannot work the door")
    if problems:
        raise DayValidationError("; ".join(problems))

    excluded = {str(eid) for eid, e in employees.items() if e["pool_role"] == "EXCLUDED"}
    door_w = Fraction(str(door_weight))
    try:
        result = compute_day(
            food_sales=_dollars(inputs["food_sales_cents"]),
            event_food_sales=_dollars(inputs["event_food_sales_cents"]),
            credit_tips=_dollars(inputs["credit_tips_cents"]),
            cash_tips=_dollars(inputs["cash_tips_cents"]),
            event_tips=_dollars(inputs["event_tips_cents"]),
            auto_gratuity=_dollars(inputs["auto_gratuity_cents"]),
            boh_worked=[str(e) for e in boh_ids],
            foh_hours={str(k): v for k, v in foh_hours.items()},
            excluded=excluded,
            foh_role_weights={str(e): door_w for e in door_ids},
        )
    except ManagerInPoolError as exc:
        ids = [i for i in excluded if i in str(exc)]
        names = ", ".join(
            employees[int(i)]["display_name"] for i in ids
        ) or "an excluded employee"
        raise DayValidationError(
            f"{names} is marked excluded (manager/owner) and cannot be in any pool"
        ) from exc

    def name(eid: str) -> str:
        return employees[int(eid)]["display_name"]

    return {
        "engine_version": engine.__version__,
        "boh_food_pct": str(DEFAULT_BOH_FOOD_PCT),
        "boh_event_food_pct": str(DEFAULT_BOH_EVENT_FOOD_PCT),
        # the door weight this day was computed with, so a re-read of the
        # snapshot explains its own numbers even if the setting changes later
        # ("1/2" exact for audit; the float is for display arithmetic)
        "door_weight": str(door_w),
        "door_weight_num": float(door_w),
        # hours are credited in whole steps of this, rounded up (owner
        # 2026-07-29); the day screen mirrors it so typed hours snap on entry
        "hours_increment": float(hours_increment),
        "totals": {
            "total_tips_cents": result.total_tips_cents,
            "boh_allocation_cents": result.boh_allocation_cents,
            "foh_pool_cents": result.foh_pool_cents,
            "auto_gratuity_cents": result.auto_gratuity_cents,
            "foh_shortfall_cents": result.foh_shortfall_cents,
            # rate per WEIGHTED hour (payout == rate * weighted_hours)
            "tips_per_hour": result.tips_per_hour,
            "total_weighted_hours": result.total_weighted_hours,
        },
        "flags": result.flags,
        "foh": sorted(
            (
                {
                    "employee_id": int(eid),
                    "name": name(eid),
                    "hours": foh_hours[int(eid)],
                    "door": int(eid) in door_ids,
                    "weighted_hours": result.weighted_hours.get(eid, 0.0),
                    "tips_cents": result.foh_payout_cents[eid],
                    "gratuity_cents": result.gratuity_payout_cents[eid],
                }
                for eid in result.foh_payout_cents
            ),
            key=lambda r: r["name"],
        ),
        "boh": sorted(
            (
                {
                    "employee_id": int(eid),
                    "name": name(eid),
                    "share_cents": result.boh_payout_cents[eid],
                }
                for eid in result.boh_payout_cents
            ),
            key=lambda r: r["name"],
        ),
    }


# ---------- PERCENT_TIPOUT (La Fontana, M5) ----------

LF_ROLE_ORDER = {"SERVER": 0, "BUSSER": 1, "HOST": 2, "BOH": 3}


def compute_lf_outputs(inputs: dict, employees: dict[int, dict],
                       percentages: dict, pool_split_mode: dict,
                       no_host_min_bussers: int = 0) -> dict:
    """PERCENT_TIPOUT outputs for snapshots/UI. The unattributed-tips bucket
    is carried through as a flag: computing works (so the manager can see
    live numbers) but finalize is blocked until every unattributed cent is
    assigned to a server or marked house (never silently assigned)."""

    def name(eid: int) -> str:
        return employees[eid]["display_name"]

    problems = []
    referenced = (set(inputs["server_tips"]) | set(inputs["server_cash_tips"])
                  | set(inputs["hours"]) | set(inputs["unattributed_assignments"]))
    for eid_raw in referenced:
        eid = int(eid_raw)
        if eid not in employees:
            problems.append(f"unknown employee id {eid}")
    if problems:
        raise DayValidationError("; ".join(problems))

    def as_int_keys(d: dict) -> dict[int, int]:
        return {int(k): v for k, v in d.items()}

    server_tips = as_int_keys(inputs["server_tips"])
    server_cash = as_int_keys(inputs["server_cash_tips"])
    assignments = as_int_keys(inputs["unattributed_assignments"])
    hours = as_int_keys(inputs["hours"])

    for eid, cents in assignments.items():
        if employees[eid]["pool_role"] != "SERVER":
            problems.append(
                f"unattributed tips assigned to {name(eid)}, who is not a SERVER")
        if cents < 0:
            problems.append(f"negative assignment for {name(eid)}")
    if problems:
        raise DayValidationError("; ".join(problems))
    assigned = sum(assignments.values())
    house = inputs["unattributed_house_cents"]
    unattributed = inputs["unattributed_tips_cents"]
    # can go negative after a re-pull shrinks the bucket below what the
    # manager already assigned — a flag (blocks finalize), never a hard error
    unresolved = unattributed - assigned - house

    # roles map for everyone participating (workers + anyone holding tips)
    participants = set(hours) | set(server_tips) | set(server_cash) | set(assignments)
    roles = {str(eid): employees[eid]["pool_role"] for eid in participants
             if employees[eid]["pool_role"] != "EXCLUDED"}
    excluded = {str(eid) for eid, e in employees.items()
                if e["pool_role"] == "EXCLUDED"}

    effective_tips = {
        str(eid): server_tips.get(eid, 0) + assignments.get(eid, 0)
        for eid in set(server_tips) | set(assignments)
    }
    try:
        result = compute_day_percent_tipout(
            server_tips=effective_tips,
            server_cash_tips={str(k): v for k, v in server_cash.items()},
            auto_gratuity_cents=inputs["auto_gratuity_cents"],
            roles=roles,
            hours={str(k): v for k, v in hours.items()},
            excluded=excluded,
            percentages=percentages,
            pool_split_mode=pool_split_mode,
            no_host_flag_min_bussers=no_host_min_bussers,
        )
    except ManagerInPoolError as exc:
        ids = [i for i in excluded if i in str(exc)]
        names = ", ".join(name(int(i)) for i in ids) or "an excluded employee"
        raise DayValidationError(
            f"{names} is marked excluded (manager/owner) and cannot be in any pool"
        ) from exc
    except ValueError as exc:
        raise DayValidationError(str(exc)) from exc

    people = []
    for key, role in result.roles.items():
        if role == "BOH":
            continue  # kitchen is paid from the monthly pool, not daily
        eid = int(key)
        people.append({
            "employee_id": eid,
            "name": name(eid),
            "role": role,
            "hours": hours.get(eid, 0),
            "tips_cents": effective_tips.get(key, 0) + server_cash.get(eid, 0),
            "keep_cents": result.keep_cents.get(key, 0),
            "returned_cents": result.returned_cents.get(key, 0),
            "pool_share_cents": result.pool_share_cents.get(key, 0),
            "payout_cents": result.payout_cents.get(key, 0),
            "gratuity_cents": result.gratuity_cents.get(key, 0),
        })
    people.sort(key=lambda p: (LF_ROLE_ORDER.get(p["role"], 9), p["name"]))

    flags = dict(result.flags)
    flags["unattributed_tips_unresolved"] = unresolved > 0
    flags["unattributed_tips_overresolved"] = unresolved < 0

    return {
        "model": "PERCENT_TIPOUT",
        "engine_version": engine.__version__,
        "percentages": result.percentages_used,
        "totals": {
            "total_tips_cents": result.total_tips_cents,
            "auto_gratuity_cents": result.auto_gratuity_cents,
            "pool_busser_cents": result.pools["busser"]["contributed_cents"],
            "pool_host_cents": result.pools["host"]["contributed_cents"],
            "pool_boh_cents": result.pools["boh"]["contributed_cents"],
            "unattributed_unresolved_cents": unresolved,
            "house_cents": house,
        },
        "flags": flags,
        "people": people,
        "pools": result.pools,
    }


# ---------- POINTS_HOURS (Poquitos, M6) ----------

EMPTY_INPUTS_POQ = {
    "credit_tips_cents": 0,
    "cash_tips_cents": 0,
    "auto_gratuity_cents": 0,
    # [{employee_id, role, hours}] — one entry per timecard, role from the
    # Square job chosen at clock-in (owner ruling 2026-08-03)
    "shifts": [],
    # private / special event on this day (0 pool = no event)
    "event_service_charge_cents": 0,
    "event_tips_cents": 0,
}

EMPTY_INPUTS_BY_MODEL["POINTS_HOURS"] = EMPTY_INPUTS_POQ


def compute_poq_outputs(inputs: dict, employees: dict[int, dict],
                        roles: dict, job_roles: dict,
                        foh_pct="80", support_pct="3") -> dict:
    """POINTS_HOURS outputs for snapshots/UI: the daily 80/20 points pool plus,
    when the day carries event money, the event pool as well."""
    role_points = {r: Decimal(str(v["points"])) for r, v in roles.items()}
    role_side = {r: v["side"] for r, v in roles.items()}

    problems = []
    shifts = []
    for row in inputs.get("shifts") or ():
        eid = int(row["employee_id"])
        emp = employees.get(eid)
        if emp is None:
            problems.append(f"unknown employee id {eid} on a shift")
            continue
        if row["role"] not in role_points:
            problems.append(f"{emp['display_name']}: unmapped role {row['role']!r}")
            continue
        shifts.append(Shift(employee=str(eid), role=row["role"], hours=row["hours"]))
    if problems:
        raise DayValidationError("; ".join(problems))

    excluded = {str(eid) for eid, e in employees.items() if e["pool_role"] == "EXCLUDED"}
    try:
        day = compute_day_points_hours(
            credit_tips=_dollars(inputs["credit_tips_cents"]),
            cash_tips=_dollars(inputs["cash_tips_cents"]),
            auto_gratuity=_dollars(inputs["auto_gratuity_cents"]),
            shifts=shifts, role_points=role_points, role_side=role_side,
            foh_pct=Decimal(str(foh_pct)), excluded=excluded,
        )
    except ManagerInPoolError as exc:
        raise DayValidationError(str(exc)) from exc

    event_pool_cents = (int(inputs.get("event_service_charge_cents") or 0)
                        + int(inputs.get("event_tips_cents") or 0))
    event = None
    if event_pool_cents:
        event = compute_event_points_hours(
            service_charge=_dollars(int(inputs.get("event_service_charge_cents") or 0)),
            event_tips=_dollars(int(inputs.get("event_tips_cents") or 0)),
            shifts=shifts, role_points=role_points, role_side=role_side,
            foh_pct=Decimal(str(foh_pct)), support_pct=Decimal(str(support_pct)),
        )

    def name(eid: str) -> str:
        return employees[int(eid)]["display_name"]

    everyone = sorted(set(day.tips_payout_cents) | set(event.payout_cents if event else ()))
    people = sorted(
        (
            {
                "employee_id": int(eid),
                "name": name(eid),
                "side": day.side.get(eid, "EVENT"),
                "hours": day.hours.get(eid, 0.0),
                "points": day.points.get(eid, 0.0),
                "tips_cents": day.tips_payout_cents.get(eid, 0),
                "gratuity_cents": day.gratuity_payout_cents.get(eid, 0),
                "event_cents": (event.payout_cents.get(eid, 0) if event else 0),
            }
            for eid in everyone
        ),
        key=lambda r: r["name"],
    )
    out = {
        "model": "POINTS_HOURS",
        "engine_version": engine.__version__,
        "foh_pct": str(foh_pct),
        "totals": {
            "total_tips_cents": day.total_tips_cents,
            "foh_pool_cents": day.foh_pool_cents,
            "boh_pool_cents": day.boh_pool_cents,
            "auto_gratuity_cents": day.auto_gratuity_cents,
            "foh_points_total": day.foh_points_total,
            "boh_points_total": day.boh_points_total,
        },
        "flags": dict(day.flags),
        "people": people,
    }
    if event:
        out["event"] = {
            "pool_cents": event.pool_cents,
            "foh_portion_cents": event.foh_portion_cents,
            "boh_portion_cents": event.boh_portion_cents,
            "service_pool_cents": event.service_pool_cents,
            "support_group_cents": event.support_group_cents,
            "support_pct": str(support_pct),
        }
        out["flags"].update({f"event_{k}": v for k, v in event.flags.items()})
    return out
