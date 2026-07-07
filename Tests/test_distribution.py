"""Unit tests for the §2 distribution engine (CLAUDE.md §5 list)."""

from fractions import Fraction

import pytest

from engine import ManagerInPoolError, compute_day, distribute_cents, to_cents


def test_to_cents_float_precision():
    assert to_cents(622.47) == 62247
    assert to_cents(0.1) == 10
    assert to_cents("19.995") == 2000  # half-up
    assert to_cents(7) == 700


class TestConservation:
    def test_uneven_hours_and_gratuity(self):
        out = compute_day(
            food_sales=812.37,
            event_food_sales=250.00,
            credit_tips=743.21,
            cash_tips=41.50,
            event_tips=120.00,
            auto_gratuity=99.99,
            boh_worked=["A", "B", "C"],
            foh_hours={"P": 6.25, "Q": 7.0, "R": 3.5, "S": 8.0},
        )
        assert sum(out.foh_payout_cents.values()) == out.foh_pool_cents
        assert sum(out.boh_payout_cents.values()) == out.boh_allocation_cents
        assert sum(out.gratuity_payout_cents.values()) == out.auto_gratuity_cents
        # BOH allocation = 5% food + 10% event food
        assert out.boh_allocation == pytest.approx(0.05 * 812.37 + 0.10 * 250.00, abs=0.005)
        assert out.total_tips == pytest.approx(743.21 + 41.50 + 120.00, abs=0.005)
        assert out.foh_pool == pytest.approx(out.total_tips - out.boh_allocation, abs=0.005)

    def test_hours_proportional_not_per_head(self):
        out = compute_day(
            credit_tips=100.00,
            auto_gratuity=100.00,
            foh_hours={"Long": 8.0, "Short": 2.0},
        )
        # 8:2 split, both pools — NOT 50/50
        assert out.foh_payouts == {"Long": 80.00, "Short": 20.00}
        assert out.gratuity_payouts == {"Long": 80.00, "Short": 20.00}


class TestZeroHoursDay:
    def test_no_foh_hours_flags_undistributed_pool(self):
        out = compute_day(credit_tips=200.00, auto_gratuity=50.00, foh_hours={})
        assert out.foh_payouts == {}
        assert out.gratuity_payouts == {}
        assert out.tips_per_hour == 0.0
        assert out.flags["undistributed_foh_pool"]
        assert out.flags["undistributed_gratuity"]

    def test_no_foh_hours_no_money_no_flags(self):
        out = compute_day(foh_hours={})
        assert not out.flags["undistributed_foh_pool"]
        assert not out.flags["undistributed_gratuity"]


class TestZeroBohDay:
    def test_no_boh_with_allocation_flagged(self):
        out = compute_day(food_sales=500.00, credit_tips=300.00, boh_worked=[],
                          foh_hours={"P": 7.0})
        assert out.boh_payouts == {}
        assert out.flags["boh_allocation_without_boh"]
        # allocation still deducted from FOH pool; the flag drives manager review
        assert out.foh_pool == pytest.approx(300.00 - 25.00)

    def test_no_boh_no_food_ok(self):
        out = compute_day(credit_tips=300.00, boh_worked=[], foh_hours={"P": 7.0})
        assert not out.flags["boh_allocation_without_boh"]

    def test_boh_divisor_is_actual_roster_count(self):
        # §2 rule 1: divide by roster length, never an entered headcount
        out = compute_day(food_sales=600.00, credit_tips=100.00,
                          boh_worked=["A", "B", "C", "D", "E"], foh_hours={"P": 1})
        assert len(out.boh_payouts) == 5
        assert out.boh_payouts["A"] == pytest.approx(6.00)
        assert sum(out.boh_payout_cents.values()) == 3000


class TestNegativeFohPool:
    def test_flagged_not_paid_negative(self):
        # slow day: BOH allocation ($50) > total tips ($30)
        out = compute_day(food_sales=1000.00, credit_tips=30.00,
                          boh_worked=["A"], foh_hours={"P": 4.0, "Q": 4.0})
        assert out.flags["negative_foh_pool"]
        assert out.foh_pool == pytest.approx(-20.00)
        assert out.foh_payouts == {"P": 0.0, "Q": 0.0}
        assert out.foh_shortfall == pytest.approx(20.00)
        # BOH still receives its allocation
        assert out.boh_payouts["A"] == pytest.approx(50.00)

    def test_gratuity_still_distributed_on_negative_pool_day(self):
        out = compute_day(food_sales=1000.00, credit_tips=30.00, auto_gratuity=60.00,
                          boh_worked=["A"], foh_hours={"P": 2.0, "Q": 1.0})
        assert out.flags["negative_foh_pool"]
        assert out.gratuity_payouts == {"P": 40.00, "Q": 20.00}


class TestRoundingResiduals:
    def test_residual_cents_to_largest_remainder(self):
        # $1.00 over three equal 1h shifts -> 34/33/33, extra cent by name asc
        out = compute_day(credit_tips=1.00, foh_hours={"B": 1, "A": 1, "C": 1})
        assert out.foh_payout_cents == {"A": 34, "B": 33, "C": 33}

    def test_tie_break_hours_desc_before_name(self):
        # pool 100c, hours 2/1/1 -> exact 50/25/25, no residual
        # pool 101c -> exact 50.5/25.25/25.25; floors 50/25/25, residual 1
        # largest remainder is Z (0.5) despite name order
        out = compute_day(credit_tips=1.01, foh_hours={"A": 1, "Z": 2, "B": 1})
        assert out.foh_payout_cents == {"Z": 51, "A": 25, "B": 25}

    def test_deterministic_across_dict_order(self):
        h1 = {"A": 3.25, "B": 3.25, "C": 3.25, "D": 3.25}
        h2 = dict(reversed(list(h1.items())))
        out1 = compute_day(credit_tips=100.01, foh_hours=h1)
        out2 = compute_day(credit_tips=100.01, foh_hours=h2)
        assert out1.foh_payout_cents == out2.foh_payout_cents

    def test_boh_even_split_residual_by_name(self):
        out = compute_day(food_sales=530.00, boh_worked=["Mateo", "Benito", "Juan"])
        # $26.50 / 3 -> 8.84 to alphabetically-first on equal remainders
        assert out.boh_payout_cents == {"Benito": 884, "Juan": 883, "Mateo": 883}

    def test_distribute_exactness_property(self):
        weights = {f"e{i}": Fraction(str(h)) for i, h in
                   enumerate([7.0, 6.5, 5.25, 8.0, 3.75, 7.0, 2.5])}
        for pool in [0, 1, 97, 59597, 123457, 999999]:
            shares = distribute_cents(pool, weights)
            assert sum(shares.values()) == pool


class TestManagerHardBlock:
    def test_manager_in_foh_pool_raises(self):
        with pytest.raises(ManagerInPoolError):
            compute_day(credit_tips=100, foh_hours={"P": 7.0, "Boss": 5.0},
                        excluded={"Boss"})

    def test_manager_in_boh_roster_raises(self):
        with pytest.raises(ManagerInPoolError):
            compute_day(food_sales=100, boh_worked=["A", "Owner"],
                        excluded={"Owner"})

    def test_excluded_not_working_is_fine(self):
        out = compute_day(credit_tips=100, foh_hours={"P": 7.0}, excluded={"Boss"})
        assert out.foh_payouts == {"P": 100.00}


class TestValidation:
    def test_negative_hours_rejected(self):
        with pytest.raises(ValueError):
            compute_day(foh_hours={"P": -1})

    def test_negative_sales_rejected(self):
        with pytest.raises(ValueError):
            compute_day(food_sales=-10)

    def test_duplicate_boh_rejected(self):
        with pytest.raises(ValueError):
            compute_day(boh_worked=["A", "A"])

    def test_zero_hour_employee_gets_zero(self):
        out = compute_day(credit_tips=100, foh_hours={"P": 5.0, "Cut": 0})
        assert out.foh_payouts["Cut"] == 0.0
        assert out.foh_payouts["P"] == 100.00


class TestRoleWeightHook:
    def test_default_weights_equal_per_hour(self):
        base = compute_day(credit_tips=90, foh_hours={"P": 3, "Q": 6})
        weighted = compute_day(credit_tips=90, foh_hours={"P": 3, "Q": 6},
                               foh_role_weights={"P": 1, "Q": 1})
        assert base.foh_payout_cents == weighted.foh_payout_cents

    def test_future_role_weighting_supported(self):
        out = compute_day(credit_tips=90, foh_hours={"P": 3, "Q": 3},
                          foh_role_weights={"P": 2})  # Q defaults to 1
        assert out.foh_payouts == {"P": 60.00, "Q": 30.00}
