"""POINTS_HOURS tip model — Poquitos (M6).

The whole day's tips are pooled, split by a fixed percentage between front
and back of house, and each side is then shared out by POINTS x HOURS:

    foh_pool  = round(total_tips * foh_pct)
    boh_pool  = total_tips - foh_pool          # exact remainder, never rounded twice
    points[e] = SUM over that employee's shifts of role_points[role] * hours
    payout[e] = pool * points[e] / SUM(points on that side)

Role comes from the SHIFT, not the person: Square records the job chosen at
clock-in on every timecard, so someone who bartends Friday (1.25/h) and hosts
Saturday (0.5/h) is credited correctly for each, and a split night lands in
both buckets. That is why the input is a list of (employee, role, hours)
entries rather than a per-person hours map.

Money is integer cents throughout and every pool is distributed with the same
largest-remainder method used elsewhere, so payouts always sum to the pool
exactly. See docs/M6-poquitos.md for the policy this encodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Iterable, Mapping

from .core import ManagerInPoolError, _as_fraction, _cents, distribute_cents, to_cents

# Poquitos policy: 80% of pooled tips to FOH, the remainder to BOH.
DEFAULT_POQ_FOH_PCT = Decimal("80")

# Points per hour worked, by role (policy "1:1" and ".5:1" tables).
DEFAULT_ROLE_POINTS = {
    # front of house
    "BARTENDER": Decimal("1.25"),
    "SHIFT_LEAD": Decimal("1.25"),
    "SERVER": Decimal("1"),
    "BARBACK": Decimal("0.5"),
    "BAR_PREP": Decimal("0.5"),
    "HOST": Decimal("0.5"),
    "BUSSER": Decimal("0.5"),
    "EXPEDITOR": Decimal("0.5"),
    "FOOD_RUNNER": Decimal("0.5"),
    # back of house — all 1.0, i.e. the BOH pool is simply hours-proportional
    "SOUS_CHEF": Decimal("1"),
    "LINE_COOK": Decimal("1"),
    "PREP_COOK": Decimal("1"),
    "DISHWASHER": Decimal("1"),
}

DEFAULT_ROLE_SIDE = {
    "BARTENDER": "FOH", "SHIFT_LEAD": "FOH", "SERVER": "FOH", "BARBACK": "FOH",
    "BAR_PREP": "FOH", "HOST": "FOH", "BUSSER": "FOH", "EXPEDITOR": "FOH",
    "FOOD_RUNNER": "FOH",
    "SOUS_CHEF": "BOH", "LINE_COOK": "BOH", "PREP_COOK": "BOH",
    "DISHWASHER": "BOH",
    # Side "EVENT" is neither daily pool: Poquitos uses separate Square job
    # titles for event work, and the policy puts that staff in the event pool
    # ONLY. Because the daily engine selects by side, these hours drop out of
    # the daily split automatically (owner ruling 2026-08-03).
    "EVENT_SERVER": "EVENT", "EVENT_BARTENDER": "EVENT",
    # Side "EXCLUDED" earns nothing anywhere. Exclusion at Poquitos is a
    # property of the JOB, not the person (owner ruling 2026-08-13): someone
    # who bartends one night and runs a manager shift the next earns on the
    # bartender hours only. Selecting by side gives that for free.
    "SHIFT_MANAGER": "EXCLUDED", "KITCHEN_MANAGER": "EXCLUDED",
    "OWNER": "EXCLUDED", "JANITORIAL": "EXCLUDED",
    "STAFF_TRAINER": "EXCLUDED", "TRAINING_SHIFT": "EXCLUDED",
}

# Roles that earn nothing still need a points entry so they are "known" and
# do not trip UnknownRoleError — the value is never used.
DEFAULT_ROLE_POINTS.update({
    r: Decimal("0") for r, side in DEFAULT_ROLE_SIDE.items() if side == "EXCLUDED"
})

# Event point values reuse the daily rates for the same work.
DEFAULT_ROLE_POINTS_EVENT = {
    "EVENT_BARTENDER": Decimal("1.25"),
    "EVENT_SERVER": Decimal("1"),
}
DEFAULT_ROLE_POINTS.update(DEFAULT_ROLE_POINTS_EVENT)

# Support tip-out: 3% of the event's FOH portion PER ROLE GROUP (not per
# person), each group's share then shared among everyone who worked that role
# that day — they stay in the daily pool too (owner ruling 2026-08-03).
DEFAULT_SUPPORT_TIPOUT_PCT = Decimal("3")
DEFAULT_SUPPORT_GROUPS = {
    "BUSSER": ("BUSSER",),
    "EXPO": ("EXPEDITOR", "FOOD_RUNNER"),   # policy pairs "expo/food runner"
    "HOST": ("HOST",),
}

# A pool-excluded person (manager) may still earn on a shift worked in one of
# these roles. Poquitos policy: "Bar Managers may be included in the tip pool
# when working Bartender shifts." Deliberately a narrow allowlist — every
# other manager shift stays hard-blocked.
DEFAULT_MANAGER_POOL_ROLES = ("BARTENDER",)


class UnknownRoleError(ValueError):
    """A shift carried a role with no points mapping. Never guess a rate —
    the day is blocked until the job title is mapped in Setup."""


@dataclass(frozen=True)
class Shift:
    """One timecard's worth of work: who, in what role, for how long."""

    employee: str
    role: str
    hours: object = 0


@dataclass(frozen=True)
class PointsDayResult:
    total_tips: float
    foh_pool: float
    boh_pool: float
    auto_gratuity: float
    tips_payouts: dict[str, float]
    gratuity_payouts: dict[str, float]
    flags: dict[str, bool]
    total_tips_cents: int
    foh_pool_cents: int
    boh_pool_cents: int
    auto_gratuity_cents: int
    tips_payout_cents: dict[str, int]
    gratuity_payout_cents: dict[str, int]
    # what each person's share was computed from, so a payout is checkable
    points: dict[str, float] = field(default_factory=dict)
    hours: dict[str, float] = field(default_factory=dict)
    side: dict[str, str] = field(default_factory=dict)
    foh_points_total: float = 0.0
    boh_points_total: float = 0.0


def _side_weights(shifts, role_points, role_side, want_side):
    """employee -> total points on `want_side`, plus their hours there."""
    weights: dict[str, Fraction] = {}
    hours: dict[str, Fraction] = {}
    for s in shifts:
        if role_side[s.role] != want_side:
            continue
        h = _as_fraction(s.hours)
        pts = h * _as_fraction(role_points[s.role])
        weights[s.employee] = weights.get(s.employee, Fraction(0)) + pts
        hours[s.employee] = hours.get(s.employee, Fraction(0)) + h
    return weights, hours


def compute_day_points_hours(
    *,
    credit_tips=0,
    cash_tips=0,
    auto_gratuity=0,
    shifts: Iterable[Shift] = (),
    role_points: Mapping[str, object] | None = None,
    role_side: Mapping[str, str] | None = None,
    foh_pct=DEFAULT_POQ_FOH_PCT,
    excluded: Iterable[str] = (),
    manager_pool_roles: Iterable[str] = DEFAULT_MANAGER_POOL_ROLES,
) -> PointsDayResult:
    """Compute one Poquitos day. `shifts` are already clipped/rounded hours.

    `excluded` is the manager hard-block. A manager's shift is allowed into
    the pool only when its role is in `manager_pool_roles` (Bar Manager
    bartending); any other manager shift raises ManagerInPoolError.
    """
    role_points = dict(role_points or DEFAULT_ROLE_POINTS)
    role_side = dict(role_side or DEFAULT_ROLE_SIDE)
    shifts = [s for s in shifts]
    blocked = set(excluded)
    allowed_for_managers = {r.upper() for r in manager_pool_roles}

    unknown = sorted({s.role for s in shifts
                      if s.role not in role_points or s.role not in role_side})
    if unknown:
        raise UnknownRoleError(
            f"no points mapping for role(s): {', '.join(unknown)}")

    offenders = sorted({
        s.employee for s in shifts
        if s.employee in blocked and s.role.upper() not in allowed_for_managers
    })
    if offenders:
        raise ManagerInPoolError(
            "excluded (manager/owner) staff cannot be in the pool except on "
            f"{sorted(allowed_for_managers)} shifts: {offenders}"
        )

    for s in shifts:
        if _as_fraction(s.hours) < 0:
            raise ValueError(f"negative hours for {s.employee}")

    credit_cents = to_cents(credit_tips)
    cash_cents = to_cents(cash_tips)
    gratuity_cents = to_cents(auto_gratuity)
    total_cents = credit_cents + cash_cents

    # Split once, exactly: BOH takes the remainder so the two pools always
    # add back to the total no matter how the percentage rounds.
    pct = _as_fraction(foh_pct) / 100
    if not 0 <= pct <= 1:
        raise ValueError("foh_pct must be between 0 and 100")
    foh_cents = int((total_cents * pct + Fraction(1, 2)).__floor__())
    boh_cents = total_cents - foh_cents

    foh_w, foh_h = _side_weights(shifts, role_points, role_side, "FOH")
    boh_w, boh_h = _side_weights(shifts, role_points, role_side, "BOH")
    foh_total = sum(foh_w.values(), start=Fraction(0))
    boh_total = sum(boh_w.values(), start=Fraction(0))

    flags = {
        "no_foh_worked": foh_cents > 0 and foh_total == 0,
        "no_boh_worked": boh_cents > 0 and boh_total == 0,
        "negative_tips": total_cents < 0,
        "negative_gratuity": gratuity_cents < 0,
    }

    tips_payout: dict[str, int] = {}
    if total_cents >= 0:
        if foh_total > 0:
            tips_payout.update(distribute_cents(foh_cents, foh_w))
        if boh_total > 0:
            for name, c in distribute_cents(boh_cents, boh_w).items():
                tips_payout[name] = tips_payout.get(name, 0) + c

    # Auto-gratuity: its own pool on its own payroll line (service charges are
    # wages, not tips — owner ruling 2026-08-03), but shared out by the same
    # 80/20 + points mechanics so nobody's share changes shape.
    grat_payout: dict[str, int] = {}
    if gratuity_cents > 0:
        g_foh = int((gratuity_cents * pct + Fraction(1, 2)).__floor__())
        g_boh = gratuity_cents - g_foh
        if foh_total > 0:
            grat_payout.update(distribute_cents(g_foh, foh_w))
        if boh_total > 0:
            for name, c in distribute_cents(g_boh, boh_w).items():
                grat_payout[name] = grat_payout.get(name, 0) + c

    everyone = sorted(set(foh_w) | set(boh_w))
    tips_payout = {n: tips_payout.get(n, 0) for n in everyone}
    grat_payout = {n: grat_payout.get(n, 0) for n in everyone}

    # Conservation (exact by construction — assert anyway)
    if total_cents >= 0 and (foh_total > 0 or boh_total > 0):
        expected = (foh_cents if foh_total > 0 else 0) + (boh_cents if boh_total > 0 else 0)
        assert sum(tips_payout.values()) == expected
    if gratuity_cents > 0 and (foh_total > 0 or boh_total > 0):
        g_foh = int((gratuity_cents * pct + Fraction(1, 2)).__floor__())
        expected_g = ((g_foh if foh_total > 0 else 0)
                      + (gratuity_cents - g_foh if boh_total > 0 else 0))
        assert sum(grat_payout.values()) == expected_g

    sides = {}
    for n in everyone:
        sides[n] = "FOH" if foh_w.get(n) else "BOH"

    return PointsDayResult(
        total_tips=_cents(total_cents),
        foh_pool=_cents(foh_cents),
        boh_pool=_cents(boh_cents),
        auto_gratuity=_cents(gratuity_cents),
        tips_payouts={n: _cents(c) for n, c in tips_payout.items()},
        gratuity_payouts={n: _cents(c) for n, c in grat_payout.items()},
        flags=flags,
        total_tips_cents=total_cents,
        foh_pool_cents=foh_cents,
        boh_pool_cents=boh_cents,
        auto_gratuity_cents=gratuity_cents,
        tips_payout_cents=tips_payout,
        gratuity_payout_cents=grat_payout,
        points={n: float(foh_w.get(n, 0) + boh_w.get(n, 0)) for n in everyone},
        hours={n: float(foh_h.get(n, 0) + boh_h.get(n, 0)) for n in everyone},
        side=sides,
        foh_points_total=float(foh_total),
        boh_points_total=float(boh_total),
    )


# ---------- private / special events ----------


@dataclass(frozen=True)
class EventResult:
    """One event's distribution. `payouts` is the merged per-person total;
    the component dicts show which rule produced each piece."""

    pool: float
    foh_portion: float
    boh_portion: float
    service_pool: float
    payouts: dict[str, float]
    pool_cents: int
    foh_portion_cents: int
    boh_portion_cents: int
    service_pool_cents: int
    payout_cents: dict[str, int]
    service_payout_cents: dict[str, int] = field(default_factory=dict)
    support_payout_cents: dict[str, int] = field(default_factory=dict)
    boh_payout_cents: dict[str, int] = field(default_factory=dict)
    support_group_cents: dict[str, int] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)


def compute_event_points_hours(
    *,
    service_charge=0,
    event_tips=0,
    shifts: Iterable[Shift] = (),
    role_points: Mapping[str, object] | None = None,
    role_side: Mapping[str, str] | None = None,
    foh_pct=DEFAULT_POQ_FOH_PCT,
    support_pct=DEFAULT_SUPPORT_TIPOUT_PCT,
    support_groups: Mapping[str, Iterable[str]] | None = None,
) -> EventResult:
    """Distribute one private/special event (Poquitos policy, §2 of the M6 doc).

        pool         = service charge + event tips
        foh_portion  = 80% of pool          boh_portion = the remainder
        each support group takes `support_pct` of foh_portion (3% x 3 = 9%)
        service_pool = foh_portion - those shares -> event service staff

    `shifts` must be the WHOLE day's shifts, not just the event's: the support
    tip-outs go to everyone who worked that role that day, whether or not they
    were on the event. Event service staff are identified by their role's side
    being "EVENT" — the job they clocked in under.
    """
    role_points = dict(role_points or DEFAULT_ROLE_POINTS)
    role_side = dict(role_side or DEFAULT_ROLE_SIDE)
    support_groups = {k: tuple(v) for k, v in
                      (support_groups or DEFAULT_SUPPORT_GROUPS).items()}
    shifts = [s for s in shifts]

    unknown = sorted({s.role for s in shifts
                      if s.role not in role_points or s.role not in role_side})
    if unknown:
        raise UnknownRoleError(
            f"no points mapping for role(s): {', '.join(unknown)}")

    pool_cents = to_cents(service_charge) + to_cents(event_tips)
    pct = _as_fraction(foh_pct) / 100
    if not 0 <= pct <= 1:
        raise ValueError("foh_pct must be between 0 and 100")
    foh_cents = int((pool_cents * pct + Fraction(1, 2)).__floor__())
    boh_cents = pool_cents - foh_cents

    sup_pct = _as_fraction(support_pct) / 100
    if sup_pct < 0 or sup_pct * len(support_groups) > 1:
        raise ValueError("support tip-outs cannot exceed the FOH portion")

    support_payout: dict[str, int] = {}
    group_cents: dict[str, int] = {}
    flags = {}
    for group, roles in sorted(support_groups.items()):
        share = int((foh_cents * sup_pct + Fraction(1, 2)).__floor__())
        # everyone who worked one of these roles TODAY, by hours (every role in
        # a group carries the same point value, so hours and points agree)
        weights: dict[str, Fraction] = {}
        for s in shifts:
            if s.role in roles:
                weights[s.employee] = weights.get(s.employee, Fraction(0)) + _as_fraction(s.hours)
        weights = {n: w for n, w in weights.items() if w > 0}
        group_cents[group] = share
        if not weights:
            # nobody in that role worked: zeroing the group leaves the share
            # inside service_cents below, so it stays with the event's FOH
            # staff rather than vanishing
            flags[f"no_{group.lower()}_worked"] = True
            group_cents[group] = 0
            continue
        for name, c in distribute_cents(share, weights).items():
            support_payout[name] = support_payout.get(name, 0) + c

    service_cents = foh_cents - sum(group_cents.values())

    # event service staff = whoever clocked in under an EVENT-side role
    service_w: dict[str, Fraction] = {}
    for s in shifts:
        if role_side[s.role] != "EVENT":
            continue
        pts = _as_fraction(s.hours) * _as_fraction(role_points[s.role])
        service_w[s.employee] = service_w.get(s.employee, Fraction(0)) + pts
    service_w = {n: w for n, w in service_w.items() if w > 0}

    service_payout: dict[str, int] = {}
    if service_w:
        service_payout = distribute_cents(service_cents, service_w)
    else:
        flags["no_event_service_staff"] = True

    # kitchen: hours worked that day (all BOH roles are 1.0, so hours == points)
    boh_w: dict[str, Fraction] = {}
    for s in shifts:
        if role_side[s.role] == "BOH":
            boh_w[s.employee] = boh_w.get(s.employee, Fraction(0)) + _as_fraction(s.hours)
    boh_w = {n: w for n, w in boh_w.items() if w > 0}
    boh_payout: dict[str, int] = {}
    if boh_w:
        boh_payout = distribute_cents(boh_cents, boh_w)
    else:
        flags["no_boh_worked"] = True

    merged: dict[str, int] = {}
    for part in (service_payout, support_payout, boh_payout):
        for name, c in part.items():
            merged[name] = merged.get(name, 0) + c

    # Conservation: everything handed out equals the pool, minus only the
    # slices that genuinely had nobody to pay. An unclaimed support share is
    # NOT one of them — it is already inside service_cents (its group was
    # zeroed above), so counting it here too would double-count it.
    undistributed = 0
    if not service_w:
        undistributed += service_cents
    if not boh_w:
        undistributed += boh_cents
    assert sum(merged.values()) == pool_cents - undistributed

    return EventResult(
        pool=_cents(pool_cents),
        foh_portion=_cents(foh_cents),
        boh_portion=_cents(boh_cents),
        service_pool=_cents(service_cents),
        payouts={n: _cents(c) for n, c in merged.items()},
        pool_cents=pool_cents,
        foh_portion_cents=foh_cents,
        boh_portion_cents=boh_cents,
        service_pool_cents=service_cents,
        payout_cents=merged,
        service_payout_cents=service_payout,
        support_payout_cents=support_payout,
        boh_payout_cents=boh_payout,
        support_group_cents=group_cents,
        flags=flags,
    )
