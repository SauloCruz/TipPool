"""Daily tip-pool distribution (CLAUDE.md §2).

All arithmetic is exact: dollars become integer cents, hours become
`Fraction`s, and payouts are allocated with a deterministic largest-remainder
method so every pool balances to the cent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from typing import Iterable, Mapping

DEFAULT_BOH_FOOD_PCT = Decimal("0.05")
DEFAULT_BOH_EVENT_FOOD_PCT = Decimal("0.10")


class ManagerInPoolError(ValueError):
    """A pool-excluded person (manager/owner) appeared in a pool roster.

    WA law + house policy: managers are hard-blocked from every pool.
    """


def to_cents(amount) -> int:
    """Convert a dollar amount (int, float, str, Decimal) to integer cents.

    Floats are routed through str() so 622.47 becomes exactly 62247.
    """
    if isinstance(amount, int) and not isinstance(amount, bool):
        return amount * 100
    d = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _cents(c: int) -> float:
    return c / 100.0


def _as_fraction(value) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        return Fraction(str(value))
    return Fraction(value)


def distribute_cents(pool_cents: int, weights: Mapping[str, Fraction]) -> dict[str, int]:
    """Split a non-negative pool of cents proportionally to `weights`.

    Largest-remainder method: floor every exact share, then hand the residual
    cents out one at a time ordered by largest fractional remainder, ties
    broken by weight (hours) descending, then name ascending. Deterministic,
    and the results always sum to `pool_cents` exactly.
    """
    if pool_cents < 0:
        raise ValueError("distribute_cents requires a non-negative pool")
    names = sorted(weights)
    total = sum(weights.values(), start=Fraction(0))
    if total <= 0:
        raise ValueError("distribute_cents requires positive total weight")
    exact = {n: pool_cents * weights[n] / total for n in names}
    shares = {n: math.floor(exact[n]) for n in names}
    residual = pool_cents - sum(shares.values())
    # base-exact = -remainder, so ascending sort = largest remainder first
    ranked = sorted(names, key=lambda n: (shares[n] - exact[n], -weights[n], n))
    for n in ranked[:residual]:
        shares[n] += 1
    return shares


@dataclass(frozen=True)
class DayResult:
    """Computed distribution for one business day. Dollar floats mirror the
    exact cent amounts in the *_cents fields."""

    total_tips: float
    boh_allocation: float
    foh_pool: float
    auto_gratuity: float
    tips_per_hour: float
    foh_payouts: dict[str, float]
    gratuity_payouts: dict[str, float]
    boh_payouts: dict[str, float]
    flags: dict[str, bool]
    foh_shortfall: float
    total_tips_cents: int
    boh_allocation_cents: int
    foh_pool_cents: int
    auto_gratuity_cents: int
    foh_payout_cents: dict[str, int]
    gratuity_payout_cents: dict[str, int]
    boh_payout_cents: dict[str, int]
    foh_shortfall_cents: int


def compute_day(
    *,
    food_sales=0,
    event_food_sales=0,
    credit_tips=0,
    cash_tips=0,
    event_tips=0,
    auto_gratuity=0,
    boh_worked: Iterable[str] = (),
    foh_hours: Mapping[str, object] | None = None,
    excluded: Iterable[str] = (),
    boh_food_pct=DEFAULT_BOH_FOOD_PCT,
    boh_event_food_pct=DEFAULT_BOH_EVENT_FOOD_PCT,
    foh_role_weights: Mapping[str, object] | None = None,
) -> DayResult:
    """Compute one day's tip distribution per CLAUDE.md §2.

    `foh_hours` must already be tippable-clipped (see clipping.clip_timecard).
    `excluded` is the manager/owner hard-block list: any overlap with either
    roster raises ManagerInPoolError. `foh_role_weights` is the v1 no-op hook
    for future role weighting (defaults to weight 1 for everyone).
    """
    foh_hours = dict(foh_hours or {})
    boh_roster = list(boh_worked)

    if len(set(boh_roster)) != len(boh_roster):
        raise ValueError("boh_worked contains duplicate names")
    blocked = set(excluded)
    offenders = blocked & (set(foh_hours) | set(boh_roster))
    if offenders:
        raise ManagerInPoolError(
            f"excluded (manager/owner) staff cannot be in any pool: {sorted(offenders)}"
        )

    food_cents = to_cents(food_sales)
    event_food_cents = to_cents(event_food_sales)
    if food_cents < 0 or event_food_cents < 0:
        raise ValueError("sales cannot be negative")

    hours = {}
    for name, h in foh_hours.items():
        hf = _as_fraction(h)
        if hf < 0:
            raise ValueError(f"negative hours for {name}")
        hours[name] = hf
    role_weights = foh_role_weights or {}
    weights = {n: h * _as_fraction(role_weights.get(n, 1)) for n, h in hours.items()}
    total_hours = sum(hours.values(), start=Fraction(0))
    total_weight = sum(weights.values(), start=Fraction(0))

    credit_cents = to_cents(credit_tips)
    cash_cents = to_cents(cash_tips)
    event_tip_cents = to_cents(event_tips)
    gratuity_cents = to_cents(auto_gratuity)

    total_tips_cents = credit_cents + cash_cents + event_tip_cents

    def _pct_share(sales_cents: int, pct) -> Fraction:
        return sales_cents * _as_fraction(pct)

    boh_alloc_exact = _pct_share(food_cents, boh_food_pct) + _pct_share(
        event_food_cents, boh_event_food_pct
    )
    boh_alloc_cents = int((boh_alloc_exact + Fraction(1, 2)).__floor__())
    foh_pool_cents = total_tips_cents - boh_alloc_cents

    flags = {
        "negative_foh_pool": foh_pool_cents < 0,
        "boh_allocation_without_boh": boh_alloc_cents > 0 and not boh_roster,
        "undistributed_foh_pool": foh_pool_cents > 0 and total_weight == 0,
        "undistributed_gratuity": gratuity_cents > 0 and total_weight == 0,
        "negative_gratuity": gratuity_cents < 0,
    }

    # FOH tip pool — hours-proportional
    foh_shortfall_cents = 0
    if foh_pool_cents < 0:
        # Never pay negative tips: zero payouts, surface the shortfall (§2 rule 4)
        foh_payout_cents = {n: 0 for n in hours}
        foh_shortfall_cents = -foh_pool_cents
    elif total_weight > 0:
        foh_payout_cents = distribute_cents(foh_pool_cents, weights)
    else:
        foh_payout_cents = {n: 0 for n in hours}

    # Auto-gratuity — separate pool, same hours-proportional mechanics
    if gratuity_cents > 0 and total_weight > 0:
        gratuity_payout_cents = distribute_cents(gratuity_cents, weights)
    else:
        gratuity_payout_cents = {n: 0 for n in hours}

    # BOH allocation — even split across the actual roster (§2 rule 1)
    if boh_roster:
        boh_payout_cents = distribute_cents(
            boh_alloc_cents, {n: Fraction(1) for n in boh_roster}
        )
    else:
        boh_payout_cents = {}

    # Conservation invariants (§2 rule 2) — exact by construction, assert anyway
    if foh_pool_cents >= 0 and total_weight > 0:
        assert sum(foh_payout_cents.values()) == foh_pool_cents
    if gratuity_cents > 0 and total_weight > 0:
        assert sum(gratuity_payout_cents.values()) == gratuity_cents
    if boh_roster:
        assert sum(boh_payout_cents.values()) == boh_alloc_cents

    tips_per_hour = (
        float(Fraction(foh_pool_cents, 100) / total_hours) if total_hours > 0 else 0.0
    )

    return DayResult(
        total_tips=_cents(total_tips_cents),
        boh_allocation=_cents(boh_alloc_cents),
        foh_pool=_cents(foh_pool_cents),
        auto_gratuity=_cents(gratuity_cents),
        tips_per_hour=tips_per_hour,
        foh_payouts={n: _cents(c) for n, c in foh_payout_cents.items()},
        gratuity_payouts={n: _cents(c) for n, c in gratuity_payout_cents.items()},
        boh_payouts={n: _cents(c) for n, c in boh_payout_cents.items()},
        flags=flags,
        foh_shortfall=_cents(foh_shortfall_cents),
        total_tips_cents=total_tips_cents,
        boh_allocation_cents=boh_alloc_cents,
        foh_pool_cents=foh_pool_cents,
        auto_gratuity_cents=gratuity_cents,
        foh_payout_cents=foh_payout_cents,
        gratuity_payout_cents=gratuity_payout_cents,
        boh_payout_cents=boh_payout_cents,
        foh_shortfall_cents=foh_shortfall_cents,
    )
