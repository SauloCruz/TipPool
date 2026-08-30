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
    # Contract labour hours, kept OUT of foh_hours on purpose (owner
    # 2026-08-30). A contractor has no Square account, so their hours are
    # always typed — and typing them into foh_hours would mark that whole map
    # as a manager override, freezing EVERY person's hours against future
    # pulls. Its own field means a re-pull keeps updating the Square staff
    # while the contractor's typed hours stay put. {employee_id: hours}
    "contractor_hours": {},
    # employee_ids working the host/door that day -> half tip credit per hour
    # (tl_door_weight). Absent in pre-2026-07-29 snapshots; treated as empty.
    # Since 2026-08-29 this is the manual OVERRIDE: a Host job on the
    # timecard sets the weight by itself, and this is how a shift clocked in
    # under the wrong job gets fixed without editing Square.
    "door_worked": [],
    # employee_id -> exact weight string from the pull, e.g. "1/2" for a
    # door-only night or "5/6" for five floor hours and one on the door.
    # Absent on hand-entered days and on every day pulled before 2026-08-29.
    "foh_role_weights": {},
    # deposit ids ("<order_id>:<line_uid>") the manager attached to this day.
    # The money is already inside event_tips_cents; these are kept so a
    # deposit can never be attached to two events and so the day says where
    # its event tips came from.
    "event_deposit_ids": [],
}

EMPTY_INPUTS_LF = {
    "server_tips": {},            # employee_id -> cents (card tips, attributed)
    "server_cash_tips": {},       # employee_id -> cents (declared at clock-out)
    "auto_gratuity_cents": 0,
    "hours": {},                  # employee_id -> worked hours (all roles)
    # Contract labour hours, kept out of `hours` for the same reason as at
    # Tavern Law: typing into the pulled map would mark it a manager override
    # and freeze everyone's hours against the next pull. LF pools split
    # EVENLY among the role members who worked, so these hours decide
    # membership and pay, not the size of anyone's share.
    "contractor_hours": {},
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
    # Contract labour hours serve two different purposes, and conflating them
    # was a bug: for an FOH contractor the hours weigh their share of the FOH
    # pool exactly like anyone else's, but a KITCHEN contractor's share comes
    # from the even split among `boh_worked` — hours never enter it. Theirs
    # are recorded only so the app can work out what to pay them directly
    # (hours x rate), so they must NOT reach the FOH pool or every FOH share
    # would be diluted by someone who was never in it.
    contractor_hours = {int(k): v
                        for k, v in (inputs.get("contractor_hours") or {}).items()
                        if v}
    for eid, h in contractor_hours.items():
        if (employees.get(eid) or {}).get("pool_role") == "FOH":
            foh_hours[eid] = foh_hours.get(eid, 0) + h
    # door_worked is absent from snapshots predating the 2026-07-29 ruling
    door_ids = {int(e) for e in inputs.get("door_worked") or ()}
    # Weights the pull derived from each shift's Square job. The manual
    # door toggle below overrides them, which is its whole purpose.
    pulled_weights = {int(k): Fraction(str(v))
                      for k, v in (inputs.get("foh_role_weights") or {}).items()}

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
    for eid in sorted(door_ids | set(pulled_weights)):
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
            foh_role_weights={
                **{str(e): w for e, w in pulled_weights.items()},
                # a hand-marked door shift wins over whatever job was clocked
                **{str(e): door_w for e in door_ids},
            },
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
                    # "door" now means *any* reduced tip credit, from a Host
                    # job on the timecard as well as a hand-marked shift; the
                    # exact rate is on `weight` for a split night ("5/6").
                    "door": (int(eid) in door_ids
                             or int(eid) in pulled_weights),
                    "door_marked": int(eid) in door_ids,
                    "weight": str(door_w if int(eid) in door_ids
                                  else pulled_weights.get(int(eid), 1)),
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
                  | set(inputs["hours"]) | set(inputs["unattributed_assignments"])
                  | set(inputs.get("contractor_hours") or {}))
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
    # a contractor who worked is a member of their role's pool like anyone
    # else; only the route the hours arrived by differs
    for eid, h in as_int_keys(inputs.get("contractor_hours") or {}).items():
        if h:
            hours[eid] = hours.get(eid, 0) + h

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
    # how much of that arrived on a card — the processing fee is withheld from
    # this part only. Events here are often invoiced (tender EXTERNAL), where
    # a blanket fee would be wrong.
    "event_card_cents": 0,
    # inferred event window: top of the hour before the first order through
    # the moment the ticket was paid (owner 2026-08-28). Display + provenance;
    # the hours actually credited are the field below.
    "event_start": None,
    "event_end": None,
    # Poquitos has no Event Bartender job, so the bartender on duty works the
    # event on an ordinary Bartender clock-in (owner 2026-08-28). These hours
    # move out of the daily pool into the event's service pool; the rest of
    # their shift stays daily, so they earn from both.
    "event_bartender_employee_id": None,
    "event_bartender_hours": 0.0,
    # Contract labour shifts. Their own list, not `shifts`, because `shifts`
    # is what the pull writes: adding one by hand would mark the whole list a
    # manager override and freeze it against every future re-pull. The role
    # has to be picked by hand too — it normally comes off the Square job
    # chosen at clock-in, and a contractor never clocks in.
    # [{employee_id, role, hours}]
    "contractor_shifts": [],
    # net sales (ex tax/tip/service charge) — reporting only, never paid out;
    # it is the denominator of the tip rate
    "net_sales_cents": 0,
}

EMPTY_INPUTS_BY_MODEL["POINTS_HOURS"] = EMPTY_INPUTS_POQ


def _draft_event_bartender(shifts: list, inputs: dict) -> tuple[list, dict]:
    """Move the drafted bartender's event hours onto an EVENT_BARTENDER shift.

    Poquitos has no Event Bartender job in Square, so the bartender working a
    private event is clocked in as an ordinary Bartender for the whole night.
    The owner's rule (2026-08-28): they earn from BOTH pools — the hours that
    overlap the event go to the event, the rest stay in the daily pool. Moving
    the hours between roles is all that takes: EVENT_BARTENDER sits on the
    EVENT side, which the daily engine already skips.

    Returns the shifts and any flags. Asking for more event hours than the
    bartender actually clocked is capped rather than refused — the day still
    computes — but it is FLAGGED, because a finalized snapshot has to explain
    the figure it paid, and a silently shortened number explains nothing.
    """
    flags: dict[str, bool] = {}
    eid = inputs.get("event_bartender_employee_id")
    hours = float(inputs.get("event_bartender_hours") or 0)
    if not eid or hours <= 0:
        return shifts, flags
    eid = str(int(eid))
    out, moved = [], 0.0
    for sh in shifts:
        take = 0.0
        if sh.employee == eid and sh.role == "BARTENDER" and moved < hours:
            take = min(float(sh.hours), hours - moved)
            moved += take
        remaining = round(float(sh.hours) - take, 2)
        if remaining > 0 or take == 0:
            out.append(Shift(employee=sh.employee, role=sh.role, hours=remaining))
    if moved > 0:
        out.append(Shift(employee=eid, role="EVENT_BARTENDER",
                         hours=round(moved, 2)))
    if round(moved, 2) < round(hours, 2):
        # they were not on the clock as a bartender for all the hours asked for
        flags["event_bartender_hours_capped"] = True
    return out, flags


def compute_poq_outputs(inputs: dict, employees: dict[int, dict],
                        roles: dict, job_roles: dict,
                        foh_pct="80", support_pct="3", card_fee_pct="0") -> dict:
    """POINTS_HOURS outputs for snapshots/UI: the daily 80/20 points pool plus,
    when the day carries event money, the event pool as well."""
    role_points = {r: Decimal(str(v["points"])) for r, v in roles.items()}
    role_side = {r: v["side"] for r, v in roles.items()}

    problems = []
    shifts = []
    for row in list(inputs.get("shifts") or ())+ list(inputs.get("contractor_shifts") or ()):
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

    shifts, draft_flags = _draft_event_bartender(shifts, inputs)

    excluded = {str(eid) for eid, e in employees.items() if e["pool_role"] == "EXCLUDED"}
    try:
        day = compute_day_points_hours(
            credit_tips=_dollars(inputs["credit_tips_cents"]),
            cash_tips=_dollars(inputs["cash_tips_cents"]),
            auto_gratuity=_dollars(inputs["auto_gratuity_cents"]),
            card_fee_pct=Decimal(str(card_fee_pct)),
            shifts=shifts, role_points=role_points, role_side=role_side,
            foh_pct=Decimal(str(foh_pct)), excluded=excluded,
        )
    except ManagerInPoolError as exc:
        raise DayValidationError(str(exc)) from exc

    event_pool_cents = (int(inputs.get("event_service_charge_cents") or 0)
                        + int(inputs.get("event_tips_cents") or 0))
    # The card-handled portion comes from the pull; if the manager then edits
    # the event money down, the stored figure can exceed the pool. Cap it —
    # a fee on money that is not there would be wrong — and flag it, so the
    # snapshot says why the fee base is not what was pulled.
    event_card_cents = int(inputs.get("event_card_cents") or 0)
    if event_card_cents > event_pool_cents:
        event_card_cents = event_pool_cents
        draft_flags["event_card_portion_capped"] = True
    event = None
    if event_pool_cents:
        event = compute_event_points_hours(
            service_charge=_dollars(int(inputs.get("event_service_charge_cents") or 0)),
            event_tips=_dollars(int(inputs.get("event_tips_cents") or 0)),
            shifts=shifts, role_points=role_points, role_side=role_side,
            foh_pct=Decimal(str(foh_pct)), support_pct=Decimal(str(support_pct)),
            card_fee_pct=Decimal(str(card_fee_pct)),
            card_pool=_dollars(event_card_cents),
        )

    def name(eid: str) -> str:
        return employees[int(eid)]["display_name"]

    # Hours on the EVENT side are invisible to the daily result (it selects by
    # side), so collect them here — otherwise an event server shows 0.00 h on
    # the day screen and a drafted bartender appears to have worked a short
    # night rather than a split one.
    event_hours: dict[str, float] = {}
    for sh in shifts:
        if role_side.get(sh.role) == "EVENT":
            event_hours[sh.employee] = round(
                event_hours.get(sh.employee, 0.0) + float(sh.hours), 2)

    everyone = sorted(set(day.tips_payout_cents) | set(event.payout_cents if event else ())
                      | set(event_hours))
    people = sorted(
        (
            {
                "employee_id": int(eid),
                "name": name(eid),
                "side": day.side.get(eid, "EVENT"),
                "hours": day.hours.get(eid, 0.0),
                "event_hours": event_hours.get(eid, 0.0),
                "points": day.points.get(eid, 0.0),
                "tips_cents": day.tips_payout_cents.get(eid, 0),
                "gratuity_cents": day.gratuity_payout_cents.get(eid, 0),
                "event_cents": (event.payout_cents.get(eid, 0) if event else 0),
            }
            for eid in everyone
        ),
        key=lambda r: r["name"],
    )
    # Someone clocked in on an event job but no event money was entered: they
    # are out of the daily pool and there is no event pool to pay them from,
    # so they would earn nothing. Read it off the SHIFTS, not the payout rows —
    # with no event pool such a person never appears in the payouts at all.
    event_staff = sorted({
        employees[int(sh.employee)]["display_name"]
        for sh in shifts if role_side.get(sh.role) == "EVENT"
    }) if not event else []
    out = {
        "model": "POINTS_HOURS",
        "engine_version": engine.__version__,
        "foh_pct": str(foh_pct),
        # the fee rate this day was computed with, so a finalized snapshot
        # explains its own numbers even if the rate changes later
        "card_fee_pct": str(card_fee_pct),
        "totals": {
            "credit_tips_gross_cents": day.credit_tips_gross_cents,
            "card_fee_cents": day.card_fee_cents,
            "credit_tips_net_cents": day.credit_tips_gross_cents - day.card_fee_cents,
            "cash_tips_cents": int(inputs["cash_tips_cents"]),
            "net_sales_cents": int(inputs.get("net_sales_cents") or 0),
            "auto_gratuity_gross_cents": day.auto_gratuity_gross_cents,
            "gratuity_fee_cents": day.gratuity_fee_cents,
            "processing_fee_total_cents": day.card_fee_cents + day.gratuity_fee_cents,
            "total_tips_cents": day.total_tips_cents,
            "foh_pool_cents": day.foh_pool_cents,
            "boh_pool_cents": day.boh_pool_cents,
            "auto_gratuity_cents": day.auto_gratuity_cents,
            # the event pool net of its own card fee — a fourth money line
            # beside tips and gratuity, so a period can total it separately
            "event_pool_cents": event.pool_cents if event else 0,
            "event_fee_cents": event.card_fee_cents if event else 0,
            "foh_points_total": day.foh_points_total,
            "boh_points_total": day.boh_points_total,
        },
        "flags": {**day.flags, **draft_flags},
        "event_staff_unpaid": sorted(event_staff),
        "people": people,
    }
    out["flags"]["event_staff_without_event_money"] = bool(event_staff)
    if event:
        out["event"] = {
            "gross_cents": event.gross_cents,
            "card_fee_cents": event.card_fee_cents,
            "pool_cents": event.pool_cents,
            "foh_portion_cents": event.foh_portion_cents,
            "boh_portion_cents": event.boh_portion_cents,
            "service_pool_cents": event.service_pool_cents,
            "support_group_cents": event.support_group_cents,
            "support_pct": str(support_pct),
        }
        out["flags"].update({f"event_{k}": v for k, v in event.flags.items()})
    return out
