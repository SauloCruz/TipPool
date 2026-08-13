"""POINTS_HOURS engine tests — Poquitos (M6).

Encodes the Poquitos Tip Pool Policy: 80% FOH / 20% BOH, each side shared by
points x hours, role taken from the shift (Square records the job chosen at
clock-in). See docs/M6-poquitos.md.
"""

from decimal import Decimal

import pytest

from engine import (
    ManagerInPoolError,
    Shift,
    UnknownRoleError,
    compute_day_points_hours,
)


def run(**kw):
    kw.setdefault("credit_tips", 1000)
    return compute_day_points_hours(**kw)


class TestPolicyTable:
    def test_point_values_match_the_policy(self):
        """1.25 bartender/shift lead, 1.0 server, 0.5 support — one hour each
        so the payouts are the point ratios directly."""
        out = run(credit_tips=100, shifts=[
            Shift("Bar", "BARTENDER", 1), Shift("Lead", "SHIFT_LEAD", 1),
            Shift("Srv", "SERVER", 1), Shift("Bus", "BUSSER", 1),
        ])
        # FOH pool 80.00 over 1.25+1.25+1+0.5 = 4 points = $20/point
        assert out.tips_payouts == {"Bar": 25.00, "Lead": 25.00,
                                    "Srv": 20.00, "Bus": 10.00}

    def test_every_support_role_is_half_a_point(self):
        for role in ("BARBACK", "BAR_PREP", "HOST", "BUSSER", "EXPEDITOR",
                     "FOOD_RUNNER"):
            out = run(credit_tips=100, shifts=[
                Shift("Srv", "SERVER", 1), Shift("X", role, 1)])
            # server 1 pt, support 0.5 pt -> 2:1 split of the 80.00 FOH pool
            assert out.tips_payouts == {"Srv": 53.33, "X": 26.67}, role

    def test_boh_roles_are_all_one_point(self):
        """Policy: the BOH pool is shared by hours worked — every kitchen role
        is 1.0, so it reduces to hours-proportional."""
        out = run(credit_tips=100, shifts=[
            Shift("Srv", "SERVER", 1),
            Shift("Sous", "SOUS_CHEF", 2), Shift("Line", "LINE_COOK", 1),
            Shift("Prep", "PREP_COOK", 1), Shift("Dish", "DISHWASHER", 0),
        ])
        # BOH pool 20.00 over 4 hours -> 5/hour
        assert out.tips_payouts["Sous"] == 10.00
        assert out.tips_payouts["Line"] == 5.00
        assert out.tips_payouts["Prep"] == 5.00
        assert out.tips_payouts["Dish"] == 0.0


class TestPoolSplit:
    def test_eighty_twenty(self):
        out = run(credit_tips=500, cash_tips=500, shifts=[
            Shift("Srv", "SERVER", 5), Shift("Cook", "LINE_COOK", 5)])
        assert out.total_tips_cents == 100000
        assert out.foh_pool_cents == 80000
        assert out.boh_pool_cents == 20000

    def test_pools_always_re_add_to_the_total(self):
        """The FOH share is rounded once and BOH takes the remainder, so an
        awkward total can never lose or invent a cent."""
        for cents in (1, 3, 7, 99, 101, 33333, 66667):
            out = compute_day_points_hours(
                credit_tips=Decimal(cents) / 100,
                shifts=[Shift("Srv", "SERVER", 1), Shift("Cook", "LINE_COOK", 1)])
            assert out.foh_pool_cents + out.boh_pool_cents == out.total_tips_cents
            assert sum(out.tips_payout_cents.values()) == out.total_tips_cents

    def test_configurable_split(self):
        out = run(credit_tips=100, foh_pct=Decimal("70"), shifts=[
            Shift("Srv", "SERVER", 1), Shift("Cook", "LINE_COOK", 1)])
        assert out.tips_payouts == {"Srv": 70.00, "Cook": 30.00}

    def test_bad_split_rejected(self):
        with pytest.raises(ValueError):
            run(foh_pct=Decimal("140"), shifts=[Shift("A", "SERVER", 1)])


class TestRoleComesFromTheShift:
    def test_one_person_two_roles_in_a_day(self):
        """The real reason role lives on the shift: Square records the job
        chosen at clock-in, so a split night is credited at both rates."""
        out = run(credit_tips=100, shifts=[
            Shift("Dani", "BARTENDER", 4),   # 5.0 points
            Shift("Dani", "HOST", 4),        # 2.0 points
            Shift("Other", "SERVER", 7),     # 7.0 points
        ])
        assert out.points["Dani"] == 7.0
        assert out.hours["Dani"] == 8.0
        # 7 of 14 points -> half the 80.00 FOH pool
        assert out.tips_payouts["Dani"] == 40.00
        assert out.tips_payouts["Other"] == 40.00

    def test_same_role_twice_aggregates(self):
        out = run(credit_tips=100, shifts=[
            Shift("Ann", "SERVER", 3), Shift("Ann", "SERVER", 2)])
        assert out.hours["Ann"] == 5.0
        assert out.points["Ann"] == 5.0

    def test_unknown_role_is_refused_not_guessed(self):
        with pytest.raises(UnknownRoleError) as exc:
            run(shifts=[Shift("A", "SOMMELIER", 5)])
        assert "SOMMELIER" in str(exc.value)


class TestManagerRule:
    """Policy: managers are excluded, EXCEPT Bar Managers on Bartender shifts."""

    def test_bar_manager_earns_on_a_bartender_shift(self):
        out = run(credit_tips=100, excluded=["Mgr"], shifts=[
            Shift("Mgr", "BARTENDER", 4), Shift("Srv", "SERVER", 5)])
        assert out.tips_payouts["Mgr"] == 40.00      # 5 of 10 FOH points

    def test_manager_blocked_on_any_other_shift(self):
        with pytest.raises(ManagerInPoolError):
            run(excluded=["Mgr"], shifts=[Shift("Mgr", "SHIFT_LEAD", 4)])
        with pytest.raises(ManagerInPoolError):
            run(excluded=["Mgr"], shifts=[Shift("Mgr", "SERVER", 4)])

    def test_exception_is_configurable_and_can_be_switched_off(self):
        with pytest.raises(ManagerInPoolError):
            run(excluded=["Mgr"], manager_pool_roles=(),
                shifts=[Shift("Mgr", "BARTENDER", 4)])

    def test_non_manager_unaffected(self):
        out = run(credit_tips=100, shifts=[Shift("Bar", "BARTENDER", 4)])
        assert out.tips_payouts["Bar"] == 80.00


class TestGratuity:
    """Owner ruling 2026-08-03: service charges stay on their own payroll line
    (wages, not tips) but are shared by the same 80/20 + points mechanics."""

    def test_gratuity_is_a_separate_pool(self):
        out = run(credit_tips=100, auto_gratuity=50, shifts=[
            Shift("Srv", "SERVER", 1), Shift("Cook", "LINE_COOK", 1)])
        assert out.total_tips_cents == 10000          # gratuity NOT added in
        assert out.auto_gratuity_cents == 5000
        assert out.tips_payouts == {"Srv": 80.00, "Cook": 20.00}
        assert out.gratuity_payouts == {"Srv": 40.00, "Cook": 10.00}

    def test_gratuity_conserves(self):
        out = run(credit_tips=333.33, auto_gratuity=99.99, shifts=[
            Shift("A", "BARTENDER", 3), Shift("B", "BUSSER", 5),
            Shift("C", "LINE_COOK", 7)])
        assert sum(out.gratuity_payout_cents.values()) == out.auto_gratuity_cents
        assert sum(out.tips_payout_cents.values()) == out.total_tips_cents

    def test_no_gratuity_means_no_payouts(self):
        out = run(credit_tips=100, shifts=[Shift("Srv", "SERVER", 1)])
        assert out.gratuity_payouts == {"Srv": 0.0}


class TestEdgeCases:
    def test_no_boh_worked_is_flagged_not_silently_dropped(self):
        out = run(credit_tips=100, shifts=[Shift("Srv", "SERVER", 5)])
        assert out.flags["no_boh_worked"] is True
        assert out.boh_pool_cents == 2000
        # FOH still gets exactly its own pool; the BOH slice is not reassigned
        assert sum(out.tips_payout_cents.values()) == out.foh_pool_cents

    def test_no_foh_worked_is_flagged(self):
        out = run(credit_tips=100, shifts=[Shift("Cook", "LINE_COOK", 5)])
        assert out.flags["no_foh_worked"] is True
        assert out.tips_payouts["Cook"] == 20.00

    def test_nobody_worked(self):
        out = run(credit_tips=100, shifts=[])
        assert out.tips_payouts == {}
        assert out.flags["no_foh_worked"] and out.flags["no_boh_worked"]

    def test_zero_hours_person_earns_nothing(self):
        out = run(credit_tips=100, shifts=[
            Shift("Srv", "SERVER", 5), Shift("Sent", "SERVER", 0)])
        assert out.tips_payouts["Sent"] == 0.0
        assert out.tips_payouts["Srv"] == 80.00

    def test_negative_hours_rejected(self):
        with pytest.raises(ValueError):
            run(shifts=[Shift("A", "SERVER", -1)])

    def test_residual_cents_go_to_the_biggest_share(self):
        """$100.01 over three equal servers: someone must take the extra cent
        and it must be deterministic."""
        out = compute_day_points_hours(credit_tips=100.01, shifts=[
            Shift("A", "SERVER", 1), Shift("B", "SERVER", 1),
            Shift("C", "SERVER", 1), Shift("K", "LINE_COOK", 1)])
        assert sum(out.tips_payout_cents.values()) == 10001
        again = compute_day_points_hours(credit_tips=100.01, shifts=[
            Shift("C", "SERVER", 1), Shift("A", "SERVER", 1),
            Shift("B", "SERVER", 1), Shift("K", "LINE_COOK", 1)])
        assert out.tips_payout_cents == again.tips_payout_cents  # order-independent


class TestWorkedExample:
    def test_a_plausible_friday(self):
        """End-to-end sanity: a full crew, checked by hand."""
        out = compute_day_points_hours(
            credit_tips=2400, cash_tips=600, auto_gratuity=0,
            shifts=[
                Shift("Bartender A", "BARTENDER", 8),    # 10.0
                Shift("Bartender B", "BARTENDER", 6),    #  7.5
                Shift("Shift Lead", "SHIFT_LEAD", 8),    # 10.0
                Shift("Server A", "SERVER", 7),          #  7.0
                Shift("Server B", "SERVER", 5),          #  5.0
                Shift("Host", "HOST", 6),                #  3.0
                Shift("Busser", "BUSSER", 6),            #  3.0
                Shift("Line Cook", "LINE_COOK", 8),
                Shift("Dishwasher", "DISHWASHER", 6),
            ])
        assert out.total_tips_cents == 300000
        assert out.foh_pool_cents == 240000 and out.boh_pool_cents == 60000
        assert out.foh_points_total == 45.5
        # $2400 / 45.5 points = $52.7472... per point
        assert out.tips_payouts["Bartender A"] == 527.47   # 10 points
        assert out.tips_payouts["Host"] == 158.24          # 3 points
        # kitchen: 14 hours over $600 -> $42.857/hour
        assert out.tips_payouts["Line Cook"] == 342.86
        assert out.tips_payouts["Dishwasher"] == 257.14
        assert sum(out.tips_payout_cents.values()) == 300000
