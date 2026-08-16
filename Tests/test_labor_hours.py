"""Regular/overtime hours — reporting only, never part of a payout.

The two rules under test were derived by reconciling against Poquitos' live
point-of-sale for 2026-08-01..15, where together they reproduce its regular
1580.28 / overtime 16.80 / paid 1597.08 exactly. Neither rule is obvious and
each is worth real money in a fortnight, so both are pinned here.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from engine import LaborHours, split_at_midnight, weekly_overtime

TZ = ZoneInfo("America/Los_Angeles")


def dt(s):
    return datetime.fromisoformat(s)


class TestSplitAtMidnight:
    def test_a_shift_inside_one_day_is_one_piece(self):
        pieces = split_at_midnight(dt("2026-08-15T17:00:00-07:00"),
                                   dt("2026-08-15T23:30:00-07:00"), TZ)
        assert pieces == [(date(2026, 8, 15), 6.5)]

    def test_a_shift_over_midnight_splits(self):
        pieces = split_at_midnight(dt("2026-08-15T16:01:00-07:00"),
                                   dt("2026-08-16T00:06:00-07:00"), TZ)
        days = [d for d, _ in pieces]
        assert days == [date(2026, 8, 15), date(2026, 8, 16)]
        assert pieces[1][1] == pytest.approx(0.1, abs=1e-9)
        assert sum(h for _, h in pieces) == pytest.approx(8 + 5 / 60)

    def test_the_pieces_always_sum_to_the_shift(self):
        total = sum(h for _, h in split_at_midnight(
            dt("2026-08-14T22:00:00-07:00"),
            dt("2026-08-17T03:30:00-07:00"), TZ))
        assert total == pytest.approx(53.5)

    def test_a_reversed_or_empty_interval_yields_nothing(self):
        """A double-punch must not invent negative hours."""
        assert split_at_midnight(dt("2026-08-15T20:00:00-07:00"),
                                 dt("2026-08-15T20:00:00-07:00"), TZ) == []
        assert split_at_midnight(dt("2026-08-15T21:00:00-07:00"),
                                 dt("2026-08-15T20:00:00-07:00"), TZ) == []

    def test_utc_input_is_converted_before_splitting(self):
        """Square sends offsets; a naive UTC split would cut at 5 PM local."""
        pieces = split_at_midnight(dt("2026-08-16T04:00:00+00:00"),
                                   dt("2026-08-16T08:00:00+00:00"), TZ)
        assert [d for d, _ in pieces] == [date(2026, 8, 15), date(2026, 8, 16)]

    def test_a_dst_day_is_not_off_by_an_hour(self):
        """2026-11-01 has 25 local hours; clocks go back at 2 AM."""
        pieces = split_at_midnight(dt("2026-11-01T00:00:00-07:00"),
                                   dt("2026-11-02T00:00:00-08:00"), TZ)
        assert [d for d, _ in pieces] == [date(2026, 11, 1)]
        assert pieces[0][1] == pytest.approx(25.0)


class TestWeeklyOvertime:
    P0, P1 = date(2026, 8, 1), date(2026, 8, 15)

    def test_under_the_threshold_earns_none(self):
        days = [("ana", date(2026, 8, 3 + i), 7.0) for i in range(5)]
        assert weekly_overtime(days, self.P0, self.P1) == 0.0

    def test_only_the_hours_past_forty_count(self):
        days = [("ana", date(2026, 8, 3 + i), 9.0) for i in range(5)]  # 45
        assert weekly_overtime(days, self.P0, self.P1) == pytest.approx(5.0)

    def test_each_person_gets_their_own_forty(self):
        days = [(who, date(2026, 8, 3 + i), 9.0)
                for who in ("ana", "ben") for i in range(5)]
        assert weekly_overtime(days, self.P0, self.P1) == pytest.approx(10.0)

    def test_hours_before_the_period_still_count_toward_the_threshold(self):
        """The week straddling the period start must be loaded whole —
        otherwise the first week silently under-reports."""
        # Sun 7/26 .. Sat 8/1, weeks start Sunday: 38 h before August, 6 on 8/1
        days = [("ana", date(2026, 7, 27 + i), 9.5) for i in range(4)]
        days.append(("ana", date(2026, 8, 1), 6.0))
        # 44 h in the week; the 4 over the line fall on 8/1, inside the period
        assert weekly_overtime(days, self.P0, self.P1) == pytest.approx(4.0)

    def test_overtime_earned_before_the_period_stays_outside_it(self):
        """The 4 h over the line on 7/30 belong to July. Everything worked
        after the line is crossed is overtime, so 8/1's 2 h are ours."""
        days = [("ana", date(2026, 7, 27 + i), 11.0) for i in range(4)]  # 44
        days.append(("ana", date(2026, 8, 1), 2.0))
        assert weekly_overtime(days, self.P0, self.P1) == pytest.approx(2.0)

    def test_the_week_start_day_changes_the_answer(self):
        """Sunday vs Monday weeks split the same hours differently — the
        reason this is a setting and not a constant."""
        days = [("ana", date(2026, 8, 2), 20.0),   # a Sunday
                ("ana", date(2026, 8, 3), 25.0)]   # the Monday after
        sunday = weekly_overtime(days, self.P0, self.P1, week_start=6)
        monday = weekly_overtime(days, self.P0, self.P1, week_start=0)
        assert sunday == pytest.approx(5.0)   # both days in one week: 45
        assert monday == pytest.approx(0.0)   # split across two weeks

    def test_a_custom_threshold_is_honoured(self):
        days = [("ana", date(2026, 8, 3 + i), 8.0) for i in range(5)]
        assert weekly_overtime(days, self.P0, self.P1,
                               threshold=35.0) == pytest.approx(5.0)

    def test_a_single_long_shift_is_split_at_the_line(self):
        """Only the part of the crossing shift above 40 is overtime."""
        days = [("ana", date(2026, 8, 3), 38.0), ("ana", date(2026, 8, 4), 6.0)]
        assert weekly_overtime(days, self.P0, self.P1) == pytest.approx(4.0)


class TestLaborHours:
    def test_regular_is_the_remainder_and_the_parts_add_up(self):
        lh = LaborHours(1597.08, 16.80, [])
        assert lh.regular == 1580.28
        d = lh.as_dict()
        assert d["paid_hours"] == 1597.08
        assert d["regular_hours"] + d["overtime_hours"] == pytest.approx(
            d["paid_hours"])

    def test_unknown_dates_ride_along_so_a_report_can_refuse(self):
        lh = LaborHours(100.0, 0.0, ["2026-08-04"])
        assert lh.as_dict()["hours_unknown_dates"] == ["2026-08-04"]


class TestBlendedOvertimeRate:
    """Overtime is a half-time premium on the regular rate of THAT workweek —
    straight-time earnings over hours worked, computed per week. For someone
    holding two jobs at different rates the weekly rate and the period-wide
    average differ, and using the period average was wrong (found against a
    real pay run: a bartender/shift-lead was $0.73 out)."""

    P0, P1 = date(2026, 8, 1), date(2026, 8, 15)

    def test_one_rate_is_unaffected_by_which_average_is_used(self):
        from engine import period_labor
        entries = [("ana", date(2026, 8, 3 + i), 9.0, 2000) for i in range(5)]
        got = period_labor(entries, self.P0, self.P1)["ana"]
        assert got["overtime_hours"] == 5.0
        assert got["wages_cents"] == 95000        # 40x20 + 5x30
        assert got["blended_overtime"] is False

    def test_the_premium_uses_that_weeks_rate_not_the_periods(self):
        from engine import period_labor
        # week 1 (Sun 8/2): 10 h at $20 — no overtime, cheap week
        # week 2 (Sun 8/9): 42 h at $40 — 2 h over, expensive week
        entries = ([("ana", date(2026, 8, 3), 10.0, 2000)]
                   + [("ana", date(2026, 8, 10 + i), 10.5, 4000) for i in range(4)])
        got = period_labor(entries, self.P0, self.P1)["ana"]
        assert got["overtime_hours"] == 2.0
        # straight time 10x20 + 42x40 = 1880; premium 2 x 0.5 x $40 (week 2's
        # rate) = 40. A period-wide average of $36.19 would give only 36.19.
        assert got["wages_cents"] == 192000
        assert got["blended_overtime"] is True

    def test_two_rates_inside_one_week_blend(self):
        from engine import period_labor
        # 21 h at $20 and 21 h at $30 in one week = 42 h, $1050 straight time
        entries = [("ana", date(2026, 8, 3), 21.0, 2000),
                   ("ana", date(2026, 8, 4), 21.0, 3000)]
        got = period_labor(entries, self.P0, self.P1)["ana"]
        assert got["overtime_hours"] == 2.0
        # regular rate = 1050/42 = $25.00; premium = 2 x 0.5 x 25 = 25
        assert got["wages_cents"] == 107500
        assert got["blended_overtime"] is True

    def test_the_flag_needs_both_overtime_and_two_rates(self):
        from engine import period_labor
        two_rates_no_ot = [("ana", date(2026, 8, 3), 8.0, 2000),
                           ("ana", date(2026, 8, 4), 8.0, 3000)]
        assert period_labor(two_rates_no_ot, self.P0, self.P1
                            )["ana"]["blended_overtime"] is False


class TestStraightTimeUsesReportedHours:
    """Regular and overtime hours are rounded SEPARATELY, because those are
    the two figures the payroll form shows and multiplies. Rounding the
    combined total instead can land a hundredth of an hour away, and then
    the printed hours no longer explain the printed wages.

    From the live 2026-08-01..15 run: 63.1333 regular + 3.9333 overtime is
    reported as 63.13 + 3.93 = 67.06 h, but 67.0667 rounds to 67.07 as a
    single total — worth $0.21 at $21.30/h.
    """

    P0, P1 = date(2026, 8, 1), date(2026, 8, 15)

    def test_the_printed_hours_are_the_hours_priced(self):
        from engine import period_labor
        # 67.0667 h in one Sunday week at $21.30: 40 regular, 27.0667 over
        entries = [("ed", date(2026, 8, 3 + i), 67.0667 / 5, 2130)
                   for i in range(5)]
        got = period_labor(entries, self.P0, self.P1)["ed"]
        reg, ot = got["regular_hours"], got["overtime_hours"]
        assert round(reg + ot, 2) == 67.07      # the raw total rounds up
        # ...but pay is built from the two reported figures, not that total
        from decimal import ROUND_CEILING, Decimal
        expected = (Decimal(str(reg)) * Decimal("21.30")
                    + Decimal(str(ot)) * Decimal("1.5") * Decimal("21.30")
                    ).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        assert got["wages_cents"] == int(expected * 100)

    def test_a_real_pay_run_row(self):
        """Edilberto Pacheco, 2026-08-01..15: 63.13 regular, 3.93 overtime,
        one rate of $21.30."""
        from engine import period_labor
        # 23.1333 h in the first week (no overtime), 43.9333 in the second
        # (3.9333 over the line) — 67.0667 h in total
        entries = [("ed", date(2026, 8, 3), 23.1333, 2130),
                   ("ed", date(2026, 8, 10), 43.9333, 2130)]
        got = period_labor(entries, self.P0, self.P1)["ed"]
        assert got["regular_hours"] == 63.13
        assert got["overtime_hours"] == 3.93
        # 63.13 x 21.30 + 3.93 x 1.5 x 21.30 = 1470.2325 -> 1470.24
        assert got["wages_cents"] == 147024

    def test_no_overtime_means_no_change(self):
        from engine import period_labor
        entries = [("ana", date(2026, 8, 3), 20.9500, 2130)]
        got = period_labor(entries, self.P0, self.P1)["ana"]
        assert got["overtime_hours"] == 0.0
        assert got["wages_cents"] == 44624       # unchanged from the pay run
