"""Regular / overtime hours split — REPORTING ONLY.

None of this touches a tip payout. It exists so a period report can be
reconciled line-by-line against the point-of-sale's own labor figures, which
count hours a tip pool deliberately ignores (a manager's shift earns no share
but is still hours worked).

Two rules matter, both verified against 2026-08-01..15 at Poquitos, where
they reproduce Square's regular 1580.28 / overtime 16.80 / paid 1597.08
exactly:

1. **A shift is split at local midnight.** A bartender clocking out at 00:06
   puts 0.10 h on the following calendar day, not on the night they worked.
   (The tip pool does the opposite and credits the whole shift to the business
   day it belongs to — that is correct for tips and wrong for this.)
2. **Overtime is weekly**, accruing on the shift that carries the running
   weekly total past the threshold. Washington has no daily overtime rule;
   `threshold` and the week's first day are settings so other venues and rule
   changes do not need code.

Splitting at midnight is what makes the workweek boundary exact: those six
minutes after midnight fall in the *next* week when weeks start on Sunday,
which is worth 0.20 h of overtime over a fortnight.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

__all__ = ["split_at_midnight", "weekly_overtime", "LaborHours"]


class LaborHours:
    """Hours in a period: what was worked, and how much of it was overtime."""

    __slots__ = ("worked", "overtime", "unknown_dates")

    def __init__(self, worked: float, overtime: float,
                 unknown_dates: list[str]):
        self.worked = worked
        self.overtime = overtime
        self.unknown_dates = unknown_dates

    @property
    def regular(self) -> float:
        return round(self.worked - self.overtime, 2)

    def as_dict(self) -> dict:
        return {
            # "paid" deliberately, not "worked": this is the point-of-sale's
            # figure, split at midnight, and it will not equal the sum of the
            # tip pool's per-business-day hours
            "paid_hours": round(self.worked, 2),
            "overtime_hours": round(self.overtime, 2),
            "regular_hours": self.regular,
            "hours_unknown_dates": self.unknown_dates,
        }


def split_at_midnight(start: datetime, end: datetime,
                      tz: ZoneInfo) -> list[tuple[date, float]]:
    """Break one shift into (local calendar day, hours) pieces.

    A shift entirely inside one day returns a single piece; one crossing
    midnight returns a piece per day it touches. Returns nothing for a
    zero-length or reversed interval rather than inventing negative hours.
    """
    start, end = start.astimezone(tz), end.astimezone(tz)
    if end <= start:
        return []

    def midnight(d: date) -> datetime:
        return datetime.combine(d, datetime.min.time(), tz)

    pieces: list[tuple[date, float]] = []
    d = start.date()
    while midnight(d) < end:
        lo = max(start, midnight(d))
        hi = min(end, midnight(d + timedelta(days=1)))
        if hi > lo:
            # Subtracting two aware datetimes that share a tzinfo gives the
            # WALL-CLOCK difference, so a fall-back day would come out 24 h
            # instead of 25. Difference the absolute timestamps instead.
            pieces.append((d, (hi.timestamp() - lo.timestamp()) / 3600))
        d += timedelta(days=1)
    return pieces


def weekly_overtime(day_hours, period_start: date, period_end: date,
                    week_start: int = 6, threshold: float = 40.0) -> float:
    """Overtime hours falling inside the period.

    `day_hours` is an iterable of (employee key, local date, hours). It must
    cover the WHOLE workweek around the period, not just the period itself —
    hours worked before `period_start` still count toward the threshold, and
    getting that wrong silently under-reports the first week.

    `week_start` is a Python weekday index (Mon=0 … Sun=6).
    """
    weeks: dict[tuple, list[tuple[date, float]]] = defaultdict(list)
    for who, d, hours in day_hours:
        monday_offset = (d.weekday() - week_start) % 7
        weeks[(who, d - timedelta(days=monday_offset))].append((d, hours))

    overtime = 0.0
    for entries in weeks.values():
        running = 0.0
        for d, hours in sorted(entries):
            before, running = running, running + hours
            # only the part of THIS shift that sits above the threshold
            over = max(0.0, running - threshold) - max(0.0, before - threshold)
            if over and period_start <= d <= period_end:
                overtime += over
    return overtime
