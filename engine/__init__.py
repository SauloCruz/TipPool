"""Tavern Law tip-pool calculation engine (Milestone 1).

Pure calculation code only — no database, no Square API, no UI. All money is
handled in integer cents; hours as exact fractions. See CLAUDE.md §2 for the
business rules this module implements.
"""

__version__ = "1.2.0"  # 1.2: POOL_HOURS role weights live (door/host half credit)
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
)
from .payments import Payment, net_credit_tip_cents
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
    "Payment",
    "net_credit_tip_cents",
    "DEFAULT_PERCENTAGES",
    "LFDayResult",
    "compute_day_percent_tipout",
    "validate_percentages",
]
