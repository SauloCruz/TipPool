"""La Fontana tip model — PERCENT_TIPOUT (docs/M5-la-fontana.md §3).

Each server's OWN tips (card + declared cash) split 65/20/10/5 between
server keep, busser pool, host pool, and BOH pool. Pools split EVEN among
role members who worked (hours-proportional toggle exists, ships OFF).

Owner-ruled edge cases:
- No host worked (ruling updated 2026-07-06): each server's host share goes
  entirely to the busser pool — an extra busser covers host duties on those
  nights. Effective 65 server / 30 busser / 5 BOH. Flagged.
  (Supersedes the original 75/20/5 re-split from docs/M5-la-fontana.md.)
- BOH pool (ruling 2026-07-06): NOT distributed daily. It carries forward
  and is split evenly among the month's kitchen roster at payroll time
  (decided on the monthly export screen). Daily payouts therefore sum to
  total tips MINUS the carried BOH slice.
- No bussers: the busser pool returns pro-rata (exactly each server's own
  contribution) to the contributing servers. Flagged.
- Cascade: the no-host re-split applies FIRST, then the busser return.

All money integer cents; conservation asserted: every cent of tips lands
on somebody. Auto-gratuity stays a separate pool, split EVENLY among
SERVER+BUSSER+HOST who worked (never BOH) — owner ruling 2026-07-06: LF
does not track hours (single shift, everything splits per-head), so `hours`
only feeds the off-by-default HOURS_PROPORTIONAL pool toggle.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Mapping

from .core import ManagerInPoolError, distribute_cents

LF_ROLES = ("SERVER", "BUSSER", "HOST", "BOH")
POOL_BUCKETS = ("busser", "host", "boh")
DEFAULT_PERCENTAGES = {"server": "65", "busser": "20", "host": "10", "boh": "5"}
# Owner ruling (2026-07-06): a missing host's share goes entirely to the
# busser pool — the extra busser works host duties on no-host nights
NO_HOST_RESPLIT = {"server": Fraction(0), "busser": Fraction(1),
                   "boh": Fraction(0)}
BUCKET_ROLE = {"busser": "BUSSER", "host": "HOST", "boh": "BOH"}


def _pct(value) -> Fraction:
    return Fraction(str(value)) / 100


def validate_percentages(percentages: Mapping[str, object]) -> dict[str, Fraction]:
    pcts = {}
    for key in ("server", *POOL_BUCKETS):
        if key not in percentages:
            raise ValueError(f"missing percentage for {key!r}")
        p = _pct(percentages[key])
        if p < 0:
            raise ValueError(f"negative percentage for {key!r}")
        pcts[key] = p
    if sum(pcts.values()) != 1:
        raise ValueError("percentages must sum to exactly 100")
    return pcts


def _split_tips(total_cents: int, pcts: dict[str, Fraction]) -> dict[str, int]:
    """Largest-remainder 4-way split of one server's tips. Tie-break:
    remainder desc, percentage desc, bucket name asc. Conserves exactly."""
    keys = sorted(pcts)
    exact = {k: total_cents * pcts[k] for k in keys}
    shares = {k: int(exact[k]) for k in keys}  # floor (tips are >= 0)
    residual = total_cents - sum(shares.values())
    ranked = sorted(keys, key=lambda k: (shares[k] - exact[k], -pcts[k], k))
    for k in ranked[:residual]:
        shares[k] += 1
    return shares


@dataclass(frozen=True)
class LFDayResult:
    total_tips_cents: int
    auto_gratuity_cents: int
    percentages_used: dict[str, str]  # effective, post-cascade, as percent strings
    keep_cents: dict[str, int]        # per server: their 65% (or 72.5%) keep
    returned_cents: dict[str, int]    # per server: empty-pool returns
    pools: dict[str, dict]            # bucket -> contributed/returned/shares
    pool_share_cents: dict[str, int]  # per person: their share of their pool
    payout_cents: dict[str, int]      # per person: keep + returned + pool share
    gratuity_cents: dict[str, int]    # per person: separate wages pool
    flags: dict[str, bool]
    roles: dict[str, str]


def compute_day_percent_tipout(
    *,
    server_tips: Mapping[str, int],
    server_cash_tips: Mapping[str, int] | None = None,
    auto_gratuity_cents: int = 0,
    roles: Mapping[str, str],
    hours: Mapping[str, object] | None = None,
    excluded: Iterable[str] = (),
    percentages: Mapping[str, object] = DEFAULT_PERCENTAGES,
    pool_split_mode: Mapping[str, str] | None = None,
    no_host_flag_min_bussers: int = 0,
) -> LFDayResult:
    """`roles` covers everyone who worked; `server_tips`/`server_cash_tips`
    are cents keyed by server id. `hours` feeds gratuity distribution and
    the (off-by-default) hours-proportional pool toggle."""
    server_cash_tips = dict(server_cash_tips or {})
    hours = {k: Fraction(str(v)) for k, v in (hours or {}).items()}
    pool_split_mode = dict(pool_split_mode or {})
    pcts = validate_percentages(percentages)

    blocked = set(excluded)
    offenders = blocked & (set(roles) | set(server_tips) | set(server_cash_tips))
    if offenders:
        raise ManagerInPoolError(
            f"excluded (manager/owner) staff cannot be in any pool: {sorted(offenders)}")
    for person, role in roles.items():
        if role not in LF_ROLES:
            raise ValueError(f"unknown role {role!r} for {person}")
    members = {r: sorted(p for p, pr in roles.items() if pr == r) for r in LF_ROLES}
    for src in (server_tips, server_cash_tips):
        for s, cents in src.items():
            if roles.get(s) != "SERVER":
                raise ValueError(f"tips attributed to {s}, who is not a SERVER")
            if cents < 0:
                raise ValueError(f"negative tips for server {s} — resolve by override")

    tips = {s: server_tips.get(s, 0) + server_cash_tips.get(s, 0)
            for s in members["SERVER"]}
    total_tips = sum(tips.values())

    flags = {
        # informational: the re-split happened (expected on low-season days)
        "no_host_resplit": False,
        # real flag: no host AND thin busser coverage (owner-set threshold)
        "no_host_low_bussers": False,
        "busser_pool_returned_to_servers": False,
        "host_pool_returned_to_servers": False,
        "undistributed_gratuity": False,
    }

    # Cascade step 1 — no host worked: fold the host share into the other
    # buckets per the owner ruling, so a single split conserves exactly.
    effective = dict(pcts)
    if not members["HOST"] and effective["host"] > 0 and total_tips > 0:
        host_share = effective["host"]
        effective["server"] += host_share * NO_HOST_RESPLIT["server"]
        effective["busser"] += host_share * NO_HOST_RESPLIT["busser"]
        effective["boh"] += host_share * NO_HOST_RESPLIT["boh"]
        effective["host"] = Fraction(0)
        flags["no_host_resplit"] = True
        flags["no_host_low_bussers"] = (
            len(members["BUSSER"]) < no_host_flag_min_bussers)
    assert sum(effective.values()) == 1 or total_tips == 0

    # Per-server split, then cascade step 2 — pools with no recipients
    # return each server's own contribution (pro-rata by construction).
    keep = {}
    returned = {s: 0 for s in members["SERVER"]}
    contributed = {b: {} for b in POOL_BUCKETS}  # bucket -> {server: cents}
    for s in members["SERVER"]:
        split = _split_tips(tips[s], effective)
        keep[s] = split["server"]
        for bucket in POOL_BUCKETS:
            contributed[bucket][s] = split[bucket]

    pools = {}
    pool_share = {}
    for bucket in POOL_BUCKETS:
        pool_total = sum(contributed[bucket].values())
        recipients = members[BUCKET_ROLE[bucket]]
        info = {"contributed_cents": pool_total, "returned_cents": 0, "shares": {}}
        if bucket == "boh":
            # monthly pool (ruling 2026-07-06): carries to payroll, no daily
            # recipients, never returns to servers
            info["carried_cents"] = pool_total
            pools[bucket] = info
            continue
        if pool_total > 0 and not recipients:
            for s, cents in contributed[bucket].items():
                returned[s] += cents
            info["returned_cents"] = pool_total
            flags[f"{bucket}_pool_returned_to_servers"] = True
        elif recipients:
            mode = pool_split_mode.get(bucket, "EVEN")
            if mode == "HOURS_PROPORTIONAL":
                weights = {p: hours.get(p, Fraction(0)) for p in recipients}
                if sum(weights.values()) == 0:  # defensive: fall back to even
                    weights = {p: Fraction(1) for p in recipients}
            else:
                weights = {p: Fraction(1) for p in recipients}
            info["shares"] = distribute_cents(pool_total, weights)
            for p, cents in info["shares"].items():
                pool_share[p] = pool_share.get(p, 0) + cents
        pools[bucket] = info

    # Auto-gratuity: separate pool, EVEN split among front-of-house who
    # worked (owner ruling 2026-07-06 — LF tracks presence, not hours).
    front = [p for r in ("SERVER", "BUSSER", "HOST") for p in members[r]]
    gratuity = {p: 0 for p in roles}
    if auto_gratuity_cents > 0:
        if front:
            gratuity.update(distribute_cents(
                auto_gratuity_cents, {p: Fraction(1) for p in front}))
        else:
            flags["undistributed_gratuity"] = True

    payout = {}
    for person in roles:
        payout[person] = (keep.get(person, 0) + returned.get(person, 0)
                          + pool_share.get(person, 0))

    # Conservation invariants — every cent of tips lands on somebody,
    # today or in the month-end BOH payout.
    boh_carried = pools["boh"]["carried_cents"]
    assert sum(payout.values()) + boh_carried == total_tips
    for bucket, info in pools.items():
        assert (sum(info["shares"].values()) + info["returned_cents"]
                + info.get("carried_cents", 0)) == info["contributed_cents"]
    if auto_gratuity_cents > 0 and not flags["undistributed_gratuity"]:
        assert sum(gratuity.values()) == auto_gratuity_cents

    return LFDayResult(
        total_tips_cents=total_tips,
        auto_gratuity_cents=auto_gratuity_cents,
        percentages_used={k: format(float(v * 100), "g") for k, v in effective.items()},
        keep_cents=keep,
        returned_cents=returned,
        pools=pools,
        pool_share_cents=pool_share,
        payout_cents=payout,
        gratuity_cents=gratuity,
        flags=flags,
        roles=dict(roles),
    )
