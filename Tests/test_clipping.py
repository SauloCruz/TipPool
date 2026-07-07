"""§2a tippable-window clipping, day boundaries, timezone/DST handling."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from engine import Break, TippableWindow, business_day_bounds, clip_timecard

TZ = ZoneInfo("America/Los_Angeles")
WINDOW = TippableWindow()  # 17:00–24:00


def dt(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


def bounds(day):
    return WINDOW.bounds(day, TZ)


class TestWindowClipping:
    def test_prep_before_open_clipped(self):
        # the CLAUDE.md example: 3:00 PM in, 12:40 AM out -> 7.00 tippable
        w0, w1 = bounds(date(2026, 6, 16))
        out = clip_timecard(dt(2026, 6, 16, 15, 0), dt(2026, 6, 17, 0, 40),
                            window_start=w0, window_end=w1)
        assert out.tippable_hours == Decimal("7.00")
        assert out.raw_seconds == round(9.6667 * 3600)

    def test_clock_out_after_midnight_clipped(self):
        w0, w1 = bounds(date(2026, 6, 16))
        out = clip_timecard(dt(2026, 6, 16, 18, 0), dt(2026, 6, 17, 1, 30),
                            window_start=w0, window_end=w1)
        # 18:00–24:00 only
        assert out.tippable_hours == Decimal("6.00")

    def test_shift_entirely_outside_window(self):
        w0, w1 = bounds(date(2026, 6, 16))
        out = clip_timecard(dt(2026, 6, 16, 9, 0), dt(2026, 6, 16, 14, 0),
                            window_start=w0, window_end=w1)
        assert out.tippable_hours == Decimal("0.00")
        assert out.raw_seconds == 5 * 3600

    def test_shift_fully_inside_window(self):
        w0, w1 = bounds(date(2026, 6, 16))
        out = clip_timecard(dt(2026, 6, 16, 18, 0), dt(2026, 6, 16, 22, 0),
                            window_start=w0, window_end=w1)
        assert out.tippable_hours == Decimal("4.00")


class TestBreaks:
    def test_unpaid_break_straddling_window_open(self):
        # break 16:45–17:15: only the 15 min inside the window reduce tippable
        w0, w1 = bounds(date(2026, 6, 16))
        out = clip_timecard(
            dt(2026, 6, 16, 15, 0), dt(2026, 6, 16, 23, 0),
            breaks=[Break(dt(2026, 6, 16, 16, 45), dt(2026, 6, 16, 17, 15))],
            window_start=w0, window_end=w1,
        )
        # window overlap 17:00–23:00 = 6h, minus 17:00–17:15 = 5.75h
        assert out.tippable_hours == Decimal("5.75")

    def test_unpaid_break_inside_window(self):
        w0, w1 = bounds(date(2026, 6, 16))
        out = clip_timecard(
            dt(2026, 6, 16, 17, 0), dt(2026, 6, 16, 23, 0),
            breaks=[Break(dt(2026, 6, 16, 19, 0), dt(2026, 6, 16, 19, 30))],
            window_start=w0, window_end=w1,
        )
        assert out.tippable_hours == Decimal("5.50")

    def test_paid_break_not_deducted(self):
        w0, w1 = bounds(date(2026, 6, 16))
        out = clip_timecard(
            dt(2026, 6, 16, 17, 0), dt(2026, 6, 16, 23, 0),
            breaks=[Break(dt(2026, 6, 16, 19, 0), dt(2026, 6, 16, 19, 30), paid=True)],
            window_start=w0, window_end=w1,
        )
        assert out.tippable_hours == Decimal("6.00")


class TestRounding:
    def test_round_down_to_quarter_hour(self):
        w0, w1 = bounds(date(2026, 6, 16))
        out = clip_timecard(dt(2026, 6, 16, 17, 0), dt(2026, 6, 16, 21, 7),
                            window_start=w0, window_end=w1)
        assert out.tippable_hours == Decimal("4.00")  # 4h07m -> 4.00

    def test_round_up_to_quarter_hour(self):
        w0, w1 = bounds(date(2026, 6, 16))
        out = clip_timecard(dt(2026, 6, 16, 17, 0), dt(2026, 6, 16, 21, 8),
                            window_start=w0, window_end=w1)
        assert out.tippable_hours == Decimal("4.25")  # 4h08m -> 4.25

    def test_configurable_increment_and_no_rounding(self):
        w0, w1 = bounds(date(2026, 6, 16))
        args = dict(window_start=w0, window_end=w1)
        out = clip_timecard(dt(2026, 6, 16, 17, 0), dt(2026, 6, 16, 21, 7),
                            rounding_increment=Decimal("0.05"), **args)
        assert out.tippable_hours == Decimal("4.10")
        out = clip_timecard(dt(2026, 6, 16, 17, 0), dt(2026, 6, 16, 21, 7),
                            rounding_increment=None, **args)
        assert out.tippable_seconds == 4 * 3600 + 7 * 60


class TestDST:
    def test_fall_back_shift_measures_elapsed_time(self):
        # Nov 1 2026: clocks fall back at 2:00 AM. A 20:00 -> 02:00 shift is
        # 6 wall-clock hours but 7 real hours worked.
        out = clip_timecard(
            dt(2026, 10, 31, 20, 0),
            datetime(2026, 11, 1, 2, 0, fold=1, tzinfo=TZ),
            window_start=bounds(date(2026, 10, 31))[0],
            window_end=bounds(date(2026, 10, 31))[1],
        )
        assert out.raw_seconds == 7 * 3600
        assert out.tippable_hours == Decimal("4.00")  # 20:00–24:00

    def test_spring_forward_window_unaffected(self):
        # Mar 8 2026: 2:00 AM springs to 3:00. Window 17:00–24:00 still 7h real.
        w0, w1 = bounds(date(2026, 3, 8))
        assert w1.timestamp() - w0.timestamp() == 7 * 3600
        out = clip_timecard(dt(2026, 3, 8, 15, 0), dt(2026, 3, 9, 0, 30),
                            window_start=w0, window_end=w1)
        assert out.tippable_hours == Decimal("7.00")

    def test_naive_datetimes_rejected(self):
        w0, w1 = bounds(date(2026, 6, 16))
        with pytest.raises(ValueError):
            clip_timecard(datetime(2026, 6, 16, 17, 0), dt(2026, 6, 16, 22, 0),
                          window_start=w0, window_end=w1)


class TestWindowConfig:
    def test_window_bounds_wall_clock(self):
        w0, w1 = bounds(date(2026, 6, 16))
        assert (w0.hour, w0.minute) == (17, 0)
        assert w1 == dt(2026, 6, 17, 0, 0)

    def test_per_day_of_week_windows_are_just_values(self):
        weekend = TippableWindow(open_minutes=16 * 60)  # earlier open Sat/Sun
        w0, _ = weekend.bounds(date(2026, 6, 20), TZ)
        assert w0.hour == 16

    def test_invalid_window_rejected(self):
        with pytest.raises(ValueError):
            TippableWindow(open_minutes=1020, close_minutes=25 * 60)  # past midnight (v1 hard cutoff)
        with pytest.raises(ValueError):
            TippableWindow(open_minutes=1200, close_minutes=1100)


class TestBusinessDayBounds:
    def test_default_midnight_boundary(self):
        s, e = business_day_bounds(date(2026, 6, 16), TZ)
        assert s == dt(2026, 6, 16, 0, 0)
        assert e == dt(2026, 6, 17, 0, 0)

    def test_3am_cutoff(self):
        s, e = business_day_bounds(date(2026, 6, 16), TZ, cutoff_minutes=180)
        assert s == dt(2026, 6, 16, 3, 0)
        assert e == dt(2026, 6, 17, 3, 0)

    def test_dst_day_lengths(self):
        s, e = business_day_bounds(date(2026, 3, 8), TZ)  # spring forward
        assert e.timestamp() - s.timestamp() == 23 * 3600
        s, e = business_day_bounds(date(2026, 11, 1), TZ)  # fall back
        assert e.timestamp() - s.timestamp() == 25 * 3600

    def test_consecutive_days_tile_exactly(self):
        # no gap/overlap across the DST transition
        _, e1 = business_day_bounds(date(2026, 11, 1), TZ, cutoff_minutes=180)
        s2, _ = business_day_bounds(date(2026, 11, 2), TZ, cutoff_minutes=180)
        assert e1 == s2
