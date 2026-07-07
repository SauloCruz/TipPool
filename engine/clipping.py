"""Tippable-hours clipping (CLAUDE.md §2a) and business-day boundaries.

The tippable window is wall-clock local time (e.g. 17:00–24:00); durations are
measured in absolute elapsed seconds, so DST transition days come out right.
All datetimes passed in must be timezone-aware.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo as _tzinfo
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

DEFAULT_ROUNDING_INCREMENT = Decimal("0.25")


@dataclass(frozen=True)
class Break:
    start: datetime
    end: datetime
    paid: bool = False


@dataclass(frozen=True)
class ClippedHours:
    raw_hours: Decimal  # worked hours net of unpaid breaks, unclipped
    tippable_hours: Decimal  # clipped to window, rounded to the increment
    raw_seconds: int
    tippable_seconds: int


@dataclass(frozen=True)
class TippableWindow:
    """Open-to-public window as minutes after local midnight.

    Defaults to 17:00–24:00. close_minutes may be 1440 (midnight) but not
    beyond: v1 has a hard midnight cutoff (owner decision).
    """

    open_minutes: int = 17 * 60
    close_minutes: int = 24 * 60

    def __post_init__(self):
        if not 0 <= self.open_minutes < self.close_minutes <= 24 * 60:
            raise ValueError("window must satisfy 0 <= open < close <= 24:00")

    def bounds(self, business_day: date, tz: _tzinfo) -> tuple[datetime, datetime]:
        midnight = datetime.combine(business_day, time(0), tzinfo=tz)
        return (
            midnight + timedelta(minutes=self.open_minutes),
            midnight + timedelta(minutes=self.close_minutes),
        )


def business_day_bounds(
    business_day: date, tz: _tzinfo, cutoff_minutes: int = 0
) -> tuple[datetime, datetime]:
    """[start, end) of a business day; cutoff_minutes shifts both edges past
    midnight (e.g. 180 for a 3:00 AM day boundary)."""
    start = datetime.combine(business_day, time(0), tzinfo=tz) + timedelta(
        minutes=cutoff_minutes
    )
    end = datetime.combine(
        business_day + timedelta(days=1), time(0), tzinfo=tz
    ) + timedelta(minutes=cutoff_minutes)
    return start, end


def _ts(dt: datetime, label: str) -> float:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{label} must be timezone-aware")
    return dt.timestamp()


def _subtract(intervals: list[tuple[float, float]], cut: tuple[float, float]):
    out = []
    c0, c1 = cut
    for s, e in intervals:
        if c1 <= s or c0 >= e:
            out.append((s, e))
            continue
        if s < c0:
            out.append((s, c0))
        if c1 < e:
            out.append((c1, e))
    return out


def _round_hours(seconds: float, increment: Decimal | None) -> Decimal:
    hours = Decimal(round(seconds)) / Decimal(3600)
    if not increment:
        return hours
    steps = (hours / increment).to_integral_value(rounding=ROUND_HALF_UP)
    return steps * increment


def clip_timecard(
    clock_in: datetime,
    clock_out: datetime,
    breaks: Iterable[Break] = (),
    *,
    window_start: datetime,
    window_end: datetime,
    rounding_increment: Decimal | None = DEFAULT_ROUNDING_INCREMENT,
) -> ClippedHours:
    """Worked intervals (shift minus unpaid breaks) overlapped with the
    tippable window. Paid breaks are not deducted. Durations are absolute
    elapsed time (DST-safe)."""
    t_in = _ts(clock_in, "clock_in")
    t_out = _ts(clock_out, "clock_out")
    if t_out <= t_in:
        raise ValueError("clock_out must be after clock_in")
    w0 = _ts(window_start, "window_start")
    w1 = _ts(window_end, "window_end")

    worked = [(t_in, t_out)]
    for b in breaks:
        if b.paid:
            continue
        b0, b1 = _ts(b.start, "break start"), _ts(b.end, "break end")
        if b1 < b0:
            raise ValueError("break end must not precede break start")
        worked = _subtract(worked, (b0, b1))

    raw_seconds = round(sum(e - s for s, e in worked))
    tippable_seconds = round(
        sum(max(0.0, min(e, w1) - max(s, w0)) for s, e in worked)
    )
    return ClippedHours(
        raw_hours=_round_hours(raw_seconds, None),
        tippable_hours=_round_hours(tippable_seconds, rounding_increment),
        raw_seconds=raw_seconds,
        tippable_seconds=tippable_seconds,
    )
