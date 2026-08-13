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


# ---------- private / special events ----------

from engine import compute_event_points_hours  # noqa: E402


class TestEventStructure:
    """Policy: pool = 20% service charge + event tips, split 80/20; the three
    support groups each take 3% of the FOH portion; the rest goes to the event
    service staff. Owner rulings 2026-08-03."""

    CREW = [
        Shift("EvSrv", "EVENT_SERVER", 5),      # 5.00 points
        Shift("EvBar", "EVENT_BARTENDER", 5),   # 6.25 points
        Shift("Bus", "BUSSER", 6),
        Shift("Host", "HOST", 6),
        Shift("Expo", "EXPEDITOR", 6),
        Shift("Cook", "LINE_COOK", 8),
    ]

    def test_pool_splits_eighty_twenty(self):
        out = compute_event_points_hours(service_charge=2000, shifts=self.CREW)
        assert out.pool_cents == 200000
        assert out.foh_portion_cents == 160000
        assert out.boh_portion_cents == 40000

    def test_three_percent_per_group_not_per_person(self):
        out = compute_event_points_hours(service_charge=2000, shifts=self.CREW)
        # 3% of the 1600.00 FOH portion, once per group -> 48.00 each
        assert out.support_group_cents == {"BUSSER": 4800, "EXPO": 4800, "HOST": 4800}
        assert out.service_pool_cents == 160000 - 3 * 4800

    def test_a_group_share_is_divided_within_the_role(self):
        """Two bussers share ONE 3% slice between them — not 3% each."""
        out = compute_event_points_hours(service_charge=2000, shifts=[
            Shift("EvSrv", "EVENT_SERVER", 5),
            Shift("Bus1", "BUSSER", 6), Shift("Bus2", "BUSSER", 6)])
        assert out.support_payout_cents["Bus1"] == 2400
        assert out.support_payout_cents["Bus2"] == 2400
        assert out.support_group_cents["BUSSER"] == 4800

    def test_expo_and_food_runner_share_one_slice(self):
        """The policy pairs them: 'busser, expo/food runner and host'."""
        out = compute_event_points_hours(service_charge=2000, shifts=[
            Shift("EvSrv", "EVENT_SERVER", 5),
            Shift("Ex", "EXPEDITOR", 5), Shift("Run", "FOOD_RUNNER", 5)])
        assert out.support_payout_cents["Ex"] == 2400
        assert out.support_payout_cents["Run"] == 2400

    def test_service_pool_is_points_times_hours(self):
        out = compute_event_points_hours(service_charge=2000, shifts=self.CREW)
        # 1456.00 over 11.25 points
        assert out.service_payout_cents["EvBar"] == 80889   # 6.25 pts
        assert out.service_payout_cents["EvSrv"] == 64711   # 5.00 pts
        assert sum(out.service_payout_cents.values()) == out.service_pool_cents

    def test_kitchen_takes_the_twenty_by_hours(self):
        out = compute_event_points_hours(service_charge=2000, shifts=[
            Shift("EvSrv", "EVENT_SERVER", 5),
            Shift("Cook", "LINE_COOK", 6), Shift("Dish", "DISHWASHER", 2)])
        assert out.boh_payout_cents == {"Cook": 30000, "Dish": 10000}

    def test_everything_adds_back_to_the_pool(self):
        out = compute_event_points_hours(service_charge=1234.56, event_tips=789.01,
                                         shifts=self.CREW)
        assert sum(out.payout_cents.values()) == out.pool_cents

    def test_event_tips_join_the_service_charge(self):
        out = compute_event_points_hours(service_charge=1000, event_tips=500,
                                         shifts=self.CREW)
        assert out.pool_cents == 150000


class TestEventVsDailyPool:
    """The clock-in role decides which pool you are in (owner 2026-08-03)."""

    DAY = [
        Shift("EvSrv", "EVENT_SERVER", 6),   # event only
        Shift("Srv", "SERVER", 6),           # daily only
        Shift("Bus", "BUSSER", 6),           # daily AND event support tip-out
        Shift("Cook", "LINE_COOK", 6),
    ]

    def test_event_service_staff_are_out_of_the_daily_pool(self):
        day = compute_day_points_hours(credit_tips=1000, shifts=self.DAY)
        assert "EvSrv" not in day.tips_payouts
        # the daily FOH pool is shared by the server and busser only
        assert set(day.tips_payouts) == {"Srv", "Bus", "Cook"}

    def test_support_staff_stay_in_the_daily_pool_and_get_the_tip_out(self):
        day = compute_day_points_hours(credit_tips=1000, shifts=self.DAY)
        event = compute_event_points_hours(service_charge=2000, shifts=self.DAY)
        assert day.tips_payouts["Bus"] > 0        # still in the daily pool
        assert event.support_payout_cents["Bus"] > 0   # and tipped out

    def test_a_person_can_work_both_and_be_paid_from_both(self):
        shifts = [Shift("Dani", "SERVER", 4),            # daily
                  Shift("Dani", "EVENT_SERVER", 4),      # event
                  Shift("Cook", "LINE_COOK", 4)]
        day = compute_day_points_hours(credit_tips=1000, shifts=shifts)
        event = compute_event_points_hours(service_charge=1000, shifts=shifts)
        assert day.points["Dani"] == 4.0          # only the daily 4 hours count
        assert event.service_payout_cents["Dani"] > 0

    def test_support_tip_out_reaches_staff_who_never_worked_the_event(self):
        """Owner ruling: the 3% goes to everyone in that role THAT DAY."""
        event = compute_event_points_hours(service_charge=2000, shifts=[
            Shift("EvSrv", "EVENT_SERVER", 5),
            Shift("EarlyBus", "BUSSER", 8)])   # day shift, no event work
        assert event.support_payout_cents["EarlyBus"] == 4800


class TestEventEdgeCases:
    def test_missing_support_role_keeps_the_money_with_event_staff(self):
        """No busser worked: that 3% cannot vanish, so it stays in the service
        pool and the event is flagged."""
        out = compute_event_points_hours(service_charge=2000, shifts=[
            Shift("EvSrv", "EVENT_SERVER", 5), Shift("Cook", "LINE_COOK", 5)])
        assert out.flags["no_busser_worked"] is True
        assert out.support_group_cents["BUSSER"] == 0
        assert out.service_pool_cents == 160000          # nothing tipped out
        assert sum(out.payout_cents.values()) == out.pool_cents

    def test_no_event_service_staff_is_flagged(self):
        out = compute_event_points_hours(service_charge=1000, shifts=[
            Shift("Bus", "BUSSER", 5), Shift("Cook", "LINE_COOK", 5)])
        assert out.flags["no_event_service_staff"] is True

    def test_unknown_role_is_refused(self):
        with pytest.raises(UnknownRoleError):
            compute_event_points_hours(service_charge=100,
                                       shifts=[Shift("A", "MIXOLOGIST", 5)])

    def test_support_percentage_is_configurable(self):
        out = compute_event_points_hours(service_charge=2000, support_pct=Decimal("5"),
                                         shifts=TestEventStructure.CREW)
        assert out.support_group_cents["BUSSER"] == 8000     # 5% of 1600.00

    def test_impossible_support_percentage_rejected(self):
        with pytest.raises(ValueError):
            compute_event_points_hours(service_charge=100, support_pct=Decimal("40"),
                                       shifts=[Shift("A", "EVENT_SERVER", 1)])


class TestJobLevelExclusion:
    """Owner ruling 2026-08-13, after auditing the real Poquitos job list:
    Shift manager, Kitchen Manager, Owner, Janitorial, Staff Trainer and
    Training Shift earn nothing. Exclusion is a property of the JOB, not the
    person — the same someone can earn on a Bartender shift the next night."""

    EXCLUDED_JOBS = ["SHIFT_MANAGER", "KITCHEN_MANAGER", "OWNER",
                     "JANITORIAL", "STAFF_TRAINER", "TRAINING_SHIFT"]

    def test_each_excluded_job_earns_nothing(self):
        for job in self.EXCLUDED_JOBS:
            out = run(credit_tips=100, shifts=[
                Shift("Srv", "SERVER", 5), Shift("X", job, 8),
                Shift("Cook", "LINE_COOK", 5)])
            assert "X" not in out.tips_payouts, job
            assert out.tips_payouts["Srv"] == 80.00, job   # full FOH pool
            assert out.tips_payouts["Cook"] == 20.00, job

    def test_same_person_earns_on_the_tipped_shift_only(self):
        out = run(credit_tips=1000, shifts=[
            Shift("Dual", "BARTENDER", 4),        # 5.0 points — counts
            Shift("Dual", "SHIFT_MANAGER", 4),    # excluded — no points
            Shift("Srv", "SERVER", 5),            # 5.0 points
            Shift("Cook", "LINE_COOK", 5)])
        assert out.points["Dual"] == 5.0
        assert out.hours["Dual"] == 4.0           # manager hours not counted
        assert out.tips_payouts["Dual"] == 400.00
        assert out.tips_payouts["Srv"] == 400.00

    def test_excluded_jobs_do_not_dilute_the_pool(self):
        """A manager shift must not shrink anyone else's share."""
        without = run(credit_tips=100, shifts=[Shift("Srv", "SERVER", 5),
                                               Shift("Cook", "LINE_COOK", 5)])
        with_mgr = run(credit_tips=100, shifts=[Shift("Srv", "SERVER", 5),
                                                Shift("Cook", "LINE_COOK", 5),
                                                Shift("Mgr", "SHIFT_MANAGER", 9)])
        assert without.tips_payout_cents == with_mgr.tips_payout_cents

    def test_excluded_jobs_are_known_roles_not_errors(self):
        """They must map cleanly — an excluded job is a mapped job, not an
        unmapped one, so it never blocks the day."""
        out = run(credit_tips=100, shifts=[Shift("Own", "OWNER", 3),
                                           Shift("Srv", "SERVER", 3)])
        assert out.tips_payouts["Srv"] == 80.00

    def test_excluded_jobs_earn_no_gratuity_either(self):
        out = run(credit_tips=100, auto_gratuity=100, shifts=[
            Shift("Mgr", "SHIFT_MANAGER", 8), Shift("Srv", "SERVER", 5),
            Shift("Cook", "LINE_COOK", 5)])
        assert "Mgr" not in out.gratuity_payouts
        assert out.gratuity_payouts["Srv"] == 80.00

    def test_excluded_jobs_get_no_event_support_tip_out(self):
        ev = compute_event_points_hours(service_charge=2000, shifts=[
            Shift("EvSrv", "EVENT_SERVER", 5),
            Shift("Jan", "JANITORIAL", 8), Shift("Bus", "BUSSER", 5)])
        assert "Jan" not in ev.payout_cents
        assert ev.support_payout_cents["Bus"] == 4800
