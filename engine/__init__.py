"""Tavern Law tip-pool calculation engine (Milestone 1).

Pure calculation code only — no database, no Square API, no UI. All money is
handled in integer cents; hours as exact fractions. See CLAUDE.md §2 for the
business rules this module implements.
"""

__version__ = "1.3.0"  # 1.3: adds POINTS_HOURS (Poquitos)
# 1.2: POOL_HOURS role weights live (door/host half credit)
# 1.1: adds PERCENT_TIPOUT (La Fontana); POOL_HOURS unchanged

from .core import (
    DEFAULT_DOOR_WEIGHT,
    DayResult,
    ManagerInPoolError,
    compute_day,
    distribute_cents,
    to_cents,
)
from .clipping import (
    Break,
    ClippedHours,
    TippableWindow,
    business_day_bounds,
    clip_timecard,
    round_hours_up,
)
from .payments import Payment, net_credit_tip_cents
from .points_hours import (
    DEFAULT_MANAGER_POOL_ROLES,
    DEFAULT_SUPPORT_GROUPS,
    DEFAULT_SUPPORT_TIPOUT_PCT,
    EventResult,
    compute_event_points_hours,
    DEFAULT_POQ_FOH_PCT,
    DEFAULT_ROLE_POINTS,
    DEFAULT_ROLE_SIDE,
    PointsDayResult,
    Shift,
    UnknownRoleError,
    compute_day_points_hours,
)
from .percent_tipout import (
    DEFAULT_PERCENTAGES,
    LFDayResult,
    compute_day_percent_tipout,
    validate_percentages,
)

__all__ = [
    "DEFAULT_DOOR_WEIGHT",
    "DayResult",
    "ManagerInPoolError",
    "compute_day",
    "distribute_cents",
    "to_cents",
    "Break",
    "ClippedHours",
    "TippableWindow",
    "business_day_bounds",
    "clip_timecard",
    "round_hours_up",
    "Payment",
    "net_credit_tip_cents",
    "DEFAULT_MANAGER_POOL_ROLES",
    "DEFAULT_SUPPORT_GROUPS",
    "DEFAULT_SUPPORT_TIPOUT_PCT",
    "EventResult",
    "compute_event_points_hours",
    "DEFAULT_POQ_FOH_PCT",
    "DEFAULT_ROLE_POINTS",
    "DEFAULT_ROLE_SIDE",
    "PointsDayResult",
    "Shift",
    "UnknownRoleError",
    "compute_day_points_hours",
    "DEFAULT_PERCENTAGES",
    "LFDayResult",
    "compute_day_percent_tipout",
    "validate_percentages",
]
