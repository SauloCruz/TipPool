"""Reporting periods.

Tavern Law: semi-monthly (1st–15th, 16th–EOM) — CLAUDE.md §2, unchanged.
La Fontana (owner ruling 2026-07-06): two report periods —
  weekly  Friday–Thursday, used to pay out tips in cash every Friday
  monthly 1st–EOM, used to populate payroll
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

FRIDAY = 4  # date.weekday(): Mon=0 .. Fri=4

# schemes legal per tip model; the first entry is the venue's default
VENUE_SCHEMES = {
    "POOL_HOURS": ("semimonthly",),
    "PERCENT_TIPOUT": ("weekly", "monthly"),
}


def period_for(d: date) -> tuple[date, date]:
    if d.day <= 15:
        return date(d.year, d.month, 1), date(d.year, d.month, 15)
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, 16), date(d.year, d.month, last)


def weekly_period_for(d: date) -> tuple[date, date]:
    """Friday–Thursday week containing d."""
    start = d - timedelta(days=(d.weekday() - FRIDAY) % 7)
    return start, start + timedelta(days=6)


def monthly_period_for(d: date) -> tuple[date, date]:
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, 1), date(d.year, d.month, last)


def period_for_scheme(d: date, scheme: str) -> tuple[date, date]:
    if scheme == "weekly":
        return weekly_period_for(d)
    if scheme == "monthly":
        return monthly_period_for(d)
    return period_for(d)  # semimonthly


def prev_period_scheme(start: date, scheme: str) -> tuple[date, date]:
    return period_for_scheme(start - timedelta(days=1), scheme)


def next_period_scheme(end: date, scheme: str) -> tuple[date, date]:
    return period_for_scheme(end + timedelta(days=1), scheme)


def period_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def prev_period(start: date) -> tuple[date, date]:
    return period_for(start - timedelta(days=1))


def next_period(end: date) -> tuple[date, date]:
    return period_for(end + timedelta(days=1))
