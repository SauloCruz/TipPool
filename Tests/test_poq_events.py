"""Poquitos private events: telling the event's money apart from the day's,
and paying the bartender who covered it (owner rulings 2026-08-28).

The numbers here are the real 2026-08-17 event: one ticket rung under the
shared "Event Host" pin, $1,353.13 with a 20% service charge of $204.00, no
tips, tendered EXTERNAL (invoiced, so the card processor never touched it).
"""

from decimal import Decimal

import pytest

from app.compute import compute_poq_outputs
from app.settings_store import DEFAULTS
from app.square_extract import (
    extract_auto_gratuity,
    extract_credit_tips,
    extract_event_money,
    extract_timecards_poq,
)
from app.sync import _event_bartender
from engine.points_hours import Shift, compute_event_points_hours

TZ = "America/Los_Angeles"
EVENT_TM = "TM_EVENT_HOST"


def money(cents):
    return {"amount": cents, "currency": "USD"}


def order(oid, tmid, sc_cents=0, created="2026-08-17T22:08:02Z",
          closed="2026-08-18T00:34:42Z", extra_charges=()):
    return {
        "id": oid, "created_by_team_member_id": tmid,
        "ticket_name": "801 Jake Event", "created_at": created,
        "closed_at": closed,
        "service_charges": ([{"type": "AUTO_GRATUITY", "percentage": "20",
                              "applied_money": money(sc_cents)}]
                            if sc_cents else []) + list(extra_charges),
    }


def admin_fee(cents, name="Administrative Fee"):
    """The policy's 3% fee: goes to the manager who organised the event and
    never touches the staff pool. Square would carry it as a CUSTOM charge."""
    return {"type": "CUSTOM", "name": name, "percentage": "3",
            "applied_money": money(cents)}


def payment(pid, oid, total, tip=0, refunded=0, card=True):
    p = {"id": pid, "order_id": oid, "status": "COMPLETED",
         "total_money": money(total), "tip_money": money(tip)}
    if card:
        p["card_details"] = {"status": "CAPTURED"}
    else:
        p["source_type"] = "EXTERNAL"
    if refunded:
        p["refunded_money"] = money(refunded)
    return p


# ---------- telling an event apart from the day ----------

class TestEventMoneyIsSeparated:
    """A private event's 20% charge is an AUTO_GRATUITY like any other. Only
    the logon that rang it says whose money it is."""

    ORDERS = [
        order("EVT", EVENT_TM, sc_cents=20400),
        order("PDR", "TM_SERVER", sc_cents=4100),      # large party, not an event
    ]
    PAYMENTS = [
        payment("P_EVT", "EVT", 135313, card=False),   # invoiced
        payment("P_PDR", "PDR", 24600, tip=1500),
    ]

    def test_event_charge_leaves_the_daily_gratuity(self):
        ev = extract_event_money(self.ORDERS, self.PAYMENTS, EVENT_TM, TZ, {})
        assert ev["event_service_charge_cents"] == 20400
        grat = extract_auto_gratuity(self.ORDERS, {}, self.PAYMENTS,
                                     exclude_order_ids=ev["order_ids"])
        # the private-dining-room party's charge is ordinary gratuity and stays
        assert grat["auto_gratuity_cents"] == 4100

    def test_without_the_logon_the_event_is_paid_to_the_daily_pool(self):
        """The bug this closes: 2026-08-17 handed the event's $204 to the
        daily staff and paid its own servers nothing."""
        grat = extract_auto_gratuity(self.ORDERS, {}, self.PAYMENTS)
        assert grat["auto_gratuity_cents"] == 24500

    def test_event_card_tips_leave_the_daily_credit_tips(self):
        ev = extract_event_money(self.ORDERS, self.PAYMENTS, EVENT_TM, TZ, {})
        tips = extract_credit_tips(self.PAYMENTS, exclude_order_ids=ev["order_ids"])
        assert tips["credit_tips_cents"] == 1500

    def test_invoiced_event_owes_no_processing_fee(self):
        ev = extract_event_money(self.ORDERS, self.PAYMENTS, EVENT_TM, TZ, {})
        assert ev["card_cents"] == 0

    def test_card_paid_event_is_fee_bearing_in_full(self):
        orders = [order("EVT", EVENT_TM, sc_cents=20400)]
        pays = [payment("P", "EVT", 135313, tip=5000)]
        ev = extract_event_money(orders, pays, EVENT_TM, TZ, {})
        assert ev["card_cents"] == 20400 + 5000

    def test_no_logon_configured_finds_nothing(self):
        ev = extract_event_money(self.ORDERS, self.PAYMENTS, "", TZ, {})
        assert ev["order_ids"] == [] and ev["window"] is None

    def test_refund_comes_off_the_event_pool(self):
        orders = [order("EVT", EVENT_TM, sc_cents=20400)]
        pays = [payment("P", "EVT", 135313, tip=5000, refunded=135313)]
        ev = extract_event_money(orders, pays, EVENT_TM, TZ, {})
        # a fully refunded check returns its whole charge and tip
        assert ev["event_service_charge_cents"] == 0
        assert ev["event_tips_cents"] == 0


class TestNonGratuityChargesStayOutOfThePool:
    """The 3% administrative fee is charged in addition and is the organising
    manager's — pooling it would overpay every person on the event."""

    ORDERS = [order("EVT", EVENT_TM, sc_cents=20400,
                    extra_charges=[admin_fee(3060)])]
    PAYMENTS = [payment("P", "EVT", 138373, card=False)]

    def test_only_the_gratuity_reaches_the_pool(self):
        ev = extract_event_money(self.ORDERS, self.PAYMENTS, EVENT_TM, TZ, {})
        assert ev["event_service_charge_cents"] == 20400

    def test_the_held_out_charge_is_reported_not_dropped(self):
        ev = extract_event_money(self.ORDERS, self.PAYMENTS, EVENT_TM, TZ, {})
        assert [(c["name"], c["cents"]) for c in ev["other_charges"]] \
            == [("Administrative Fee", 3060)]

    def test_a_custom_charge_named_as_gratuity_is_still_staff_money(self):
        """Some venues ring gratuity as a custom charge; the configured name
        match is what says so."""
        orders = [order("EVT", EVENT_TM, sc_cents=0,
                        extra_charges=[{"type": "CUSTOM", "name": "Event Gratuity",
                                        "percentage": "20",
                                        "applied_money": money(20400)}])]
        ev = extract_event_money(orders, self.PAYMENTS, EVENT_TM, TZ,
                                 {"name_contains": "gratuity"})
        assert ev["event_service_charge_cents"] == 20400
        assert ev["other_charges"] == []

    def test_the_fee_never_reaches_a_payout(self):
        ev = extract_event_money(self.ORDERS, self.PAYMENTS, EVENT_TM, TZ, {})
        r = compute_event_points_hours(
            service_charge=ev["event_service_charge_cents"] / 100,
            shifts=[Shift("a", "EVENT_SERVER", 4), Shift("b", "LINE_COOK", 5)],
            card_pool=0)
        assert sum(r.payout_cents.values()) == 20400


class TestInferredWindow:
    def test_starts_at_the_hour_before_the_first_order_and_ends_when_paid(self):
        ev = extract_event_money([order("EVT", EVENT_TM, sc_cents=20400)],
                                 [payment("P", "EVT", 100, card=False)],
                                 EVENT_TM, TZ, {})
        # 22:08Z = 15:08 PT -> 15:00; paid 00:34Z = 17:34 PT
        assert ev["window"]["start_at"].startswith("2026-08-17T15:00:00")
        assert ev["window"]["end_at"].startswith("2026-08-17T17:34:42")

    def test_an_event_opened_and_paid_inside_one_hour_still_has_a_window(self):
        ev = extract_event_money(
            [order("EVT", EVENT_TM, sc_cents=100,
                   created="2026-08-17T22:05:00Z", closed="2026-08-17T22:20:00Z")],
            [], EVENT_TM, TZ, {})
        assert ev["window"]["start_at"] < ev["window"]["end_at"]


# ---------- the shared pin is a till, not a person ----------

def timecard(tmid, start, end, title="Bartender"):
    return {"team_member_id": tmid, "start_at": start, "end_at": end,
            "wage": {"title": title}, "declared_cash_tip_money": money(0)}


class TestSharedLogonNeverEarns:
    EMPS = {EVENT_TM: {"id": 99, "display_name": "Event Host", "pool_role": "INCLUDED"},
            "TM_A": {"id": 1, "display_name": "Ana", "pool_role": "INCLUDED"}}
    CARDS = [
        timecard(EVENT_TM, "2026-08-17T14:52:00-07:00", "2026-08-17T21:37:00-07:00",
                 title="Event Server"),
        timecard("TM_A", "2026-08-17T13:18:00-07:00", "2026-08-17T21:38:00-07:00"),
    ]

    def test_till_timecards_are_dropped(self):
        out = extract_timecards_poq(self.CARDS, self.EMPS, TZ, Decimal("0"),
                                    DEFAULTS["poq_job_roles"],
                                    ignore_tmids=[EVENT_TM])
        assert [s["employee_id"] for s in out["shifts"]] == [1]

    def test_without_the_ignore_the_till_takes_a_share(self):
        out = extract_timecards_poq(self.CARDS, self.EMPS, TZ, Decimal("0"),
                                    DEFAULTS["poq_job_roles"])
        assert 99 in {s["employee_id"] for s in out["shifts"]}


# ---------- drafting the bartender ----------

WINDOW = {"start_at": "2026-08-17T15:00:00-07:00",
          "end_at": "2026-08-17T17:34:42-07:00"}


def shift(eid, name, start, end, hours, role="BARTENDER"):
    return {"employee_id": eid, "name": name, "role": role, "hours": hours,
            "start_at": start, "end_at": end}


class TestEventBartenderSelection:
    def test_the_only_bartender_on_is_drafted_for_the_overlap_only(self):
        picked, cands = _event_bartender(
            [shift(1, "Ana", "2026-08-17T13:18:00-07:00",
                   "2026-08-17T21:38:00-07:00", 8.33)], WINDOW)
        assert picked["employee_id"] == 1
        assert picked["hours"] == pytest.approx(2.58)   # 15:00 -> 17:34:42 = 2.5783 h
        assert len(cands) == 1

    def test_two_bartenders_means_the_manager_decides(self):
        picked, cands = _event_bartender([
            shift(1, "Ana", "2026-08-17T13:18:00-07:00", "2026-08-17T21:38:00-07:00", 8.33),
            shift(2, "Ben", "2026-08-17T15:30:00-07:00", "2026-08-17T23:00:00-07:00", 7.5),
        ], WINDOW)
        assert picked is None
        assert [c["name"] for c in cands] == ["Ana", "Ben"]

    def test_a_bartender_who_left_before_the_event_is_not_a_candidate(self):
        picked, cands = _event_bartender(
            [shift(1, "Ana", "2026-08-17T09:00:00-07:00",
                   "2026-08-17T14:00:00-07:00", 5.0)], WINDOW)
        assert picked is None and cands == []

    def test_credited_event_hours_never_exceed_the_shift(self):
        picked, _ = _event_bartender(
            [shift(1, "Ana", "2026-08-17T13:00:00-07:00",
                   "2026-08-17T21:00:00-07:00", 1.0)], WINDOW)   # long punch, short paid
        assert picked["hours"] == 1.0

    def test_non_bartenders_are_never_drafted(self):
        picked, cands = _event_bartender(
            [shift(1, "Ana", "2026-08-17T15:00:00-07:00",
                   "2026-08-17T17:00:00-07:00", 2.0, role="SERVER")], WINDOW)
        assert picked is None and cands == []


# ---------- the whole day ----------

ROSTER = [  # real 2026-08-17 timecards, Event Host logon dropped
    ("Bartender A", "BARTENDER", 8.33), ("Busser B", "BUSSER", 4.15),
    ("Dish C", "DISHWASHER", 7.02), ("Dish D", "DISHWASHER", 5.68),
    ("EventSrv E", "EVENT_SERVER", 3.67), ("Cook F", "LINE_COOK", 7.33),
    ("Cook G", "LINE_COOK", 6.55), ("Cook H", "LINE_COOK", 5.15),
    ("Prep I", "PREP_COOK", 6.02), ("Runner J", "FOOD_RUNNER", 3.90),
    ("Server K", "SERVER", 4.73), ("Server L", "SERVER", 5.08),
    ("Mgr M", "SHIFT_MANAGER", 7.92),
]
EMPLOYEES = {i: {"display_name": n,
                 "pool_role": "EXCLUDED" if r == "SHIFT_MANAGER" else "INCLUDED"}
             for i, (n, r, _) in enumerate(ROSTER, 1)}
SHIFTS = [{"employee_id": i, "role": r, "hours": h}
          for i, (_, r, h) in enumerate(ROSTER, 1)]


def compute(**extra):
    inputs = {"credit_tips_cents": 56354, "cash_tips_cents": 8000,
              "auto_gratuity_cents": 0, "shifts": SHIFTS,
              "event_service_charge_cents": 20400, "event_tips_cents": 0,
              "event_card_cents": 0, "net_sales_cents": 0,
              "event_bartender_employee_id": None, "event_bartender_hours": 0.0}
    inputs.update(extra)
    return compute_poq_outputs(inputs, EMPLOYEES, DEFAULTS["poq_roles"],
                               DEFAULTS["poq_job_roles"])


def by_name(out, name):
    return next(p for p in out["people"] if p["name"] == name)


class TestTheAugust17Day:
    def test_event_pool_splits_80_20(self):
        e = compute()["event"]
        assert (e["pool_cents"], e["foh_portion_cents"], e["boh_portion_cents"]) \
            == (20400, 16320, 4080)

    def test_both_pools_conserve_to_the_cent(self):
        out = compute()
        assert sum(p["event_cents"] for p in out["people"]) == 20400
        assert sum(p["tips_cents"] for p in out["people"]) == 56354 + 8000

    def test_drafted_bartender_earns_from_both_pools(self):
        out = compute(event_bartender_employee_id=1, event_bartender_hours=2.58)
        bar = by_name(out, "Bartender A")
        assert bar["hours"] == 5.75 and bar["event_hours"] == 2.58
        assert bar["tips_cents"] > 0 and bar["event_cents"] > 0

    def test_drafting_moves_money_but_never_creates_it(self):
        out = compute(event_bartender_employee_id=1, event_bartender_hours=2.58)
        assert sum(p["event_cents"] for p in out["people"]) == 20400
        assert sum(p["tips_cents"] for p in out["people"]) == 56354 + 8000

    def test_drafting_lifts_everyone_elses_daily_share(self):
        """The bartender's event hours leave the daily pool, so the same daily
        money spreads over fewer points."""
        before = by_name(compute(), "Server K")["tips_cents"]
        after = by_name(compute(event_bartender_employee_id=1,
                                event_bartender_hours=2.58), "Server K")["tips_cents"]
        assert after > before

    def test_event_server_shows_their_hours(self):
        srv = by_name(compute(), "EventSrv E")
        assert srv["event_hours"] == 3.67 and srv["hours"] == 0.0

    def test_no_host_on_shift_keeps_that_3_percent_with_the_event_staff(self):
        out = compute()
        assert out["flags"]["event_no_host_worked"] is True
        assert out["event"]["support_group_cents"]["HOST"] == 0
        # 9% would be 1468; only the two staffed groups are taken out
        assert out["event"]["service_pool_cents"] == 16320 - 490 * 2

    def test_the_manager_never_shares_the_event(self):
        out = compute(event_bartender_employee_id=1, event_bartender_hours=2.58)
        assert all(p["name"] != "Mgr M" or p["event_cents"] == 0
                   for p in out["people"])


class TestEventProcessingFee:
    def test_invoiced_event_keeps_its_whole_pool(self):
        r = compute_event_points_hours(
            service_charge=204, shifts=[Shift("a", "EVENT_SERVER", 4)],
            card_fee_pct="3", card_pool=0)
        assert r.card_fee_cents == 0 and r.pool_cents == 20400

    def test_card_paid_event_bears_the_fee_before_the_split(self):
        r = compute_event_points_hours(
            service_charge=204, shifts=[Shift("a", "EVENT_SERVER", 4)],
            card_fee_pct="3")
        assert r.card_fee_cents == 612
        assert r.pool_cents == 19788 and r.gross_cents == 20400

    def test_card_portion_cannot_exceed_the_pool(self):
        with pytest.raises(ValueError):
            compute_event_points_hours(service_charge=204, card_pool=500)


class TestSilentCorrectionsAreFlagged:
    """A finalized snapshot has to explain the figure it paid, so anything the
    engine quietly caps must show up as a flag."""

    def test_more_event_hours_than_the_bartender_clocked(self):
        out = compute(event_bartender_employee_id=1, event_bartender_hours=99)
        assert out["flags"]["event_bartender_hours_capped"] is True
        # still computes, still conserves — capped, not refused
        assert sum(p["event_cents"] for p in out["people"]) == 20400

    def test_hours_within_the_shift_raise_nothing(self):
        out = compute(event_bartender_employee_id=1, event_bartender_hours=2.58)
        assert "event_bartender_hours_capped" not in out["flags"]

    def test_card_portion_larger_than_the_event_money(self):
        out = compute(event_card_cents=999999)
        assert out["flags"]["event_card_portion_capped"] is True

    def test_card_portion_within_the_pool_raises_nothing(self):
        out = compute(event_card_cents=20400)
        assert "event_card_portion_capped" not in out["flags"]


class TestTheAdministrativeFee:
    """Poquitos added an "Event Administrative Fee" to its Square account
    (2026-08-29). By policy it is the organising manager's and never touches
    the staff pool — and it must stay out however Square types it."""

    HOUSE = ["administrative fee"]
    PAYMENTS = [payment("P", "EVT", 138373, card=False)]

    def _order(self, fee_type):
        return [order("EVT", EVENT_TM, sc_cents=20400, extra_charges=[
            {"type": fee_type, "name": "Event Administrative Fee",
             "percentage": "3", "applied_money": money(3060)}])]

    def test_excluded_when_square_types_it_custom(self):
        ev = extract_event_money(self._order("CUSTOM"), self.PAYMENTS,
                                 EVENT_TM, TZ, {}, self.HOUSE)
        assert ev["event_service_charge_cents"] == 20400

    def test_excluded_even_when_square_types_it_as_gratuity(self):
        """The dashboard's gratuity flag is set by whoever made the charge; a
        mis-ticked box must not put the house's cut in the staff pool."""
        ev = extract_event_money(self._order("AUTO_GRATUITY"), self.PAYMENTS,
                                 EVENT_TM, TZ, {}, self.HOUSE)
        assert ev["event_service_charge_cents"] == 20400

    def test_without_the_house_list_a_gratuity_typed_fee_would_be_pooled(self):
        """Why the list exists: this is the failure it prevents."""
        ev = extract_event_money(self._order("AUTO_GRATUITY"), self.PAYMENTS,
                                 EVENT_TM, TZ, {}, [])
        assert ev["event_service_charge_cents"] == 20400 + 3060

    def test_it_is_reported_as_the_houses_own_charge(self):
        ev = extract_event_money(self._order("AUTO_GRATUITY"), self.PAYMENTS,
                                 EVENT_TM, TZ, {}, self.HOUSE)
        assert [(c["name"], c["cents"], c["house"]) for c in ev["other_charges"]] \
            == [("Event Administrative Fee", 3060, True)]

    def test_an_unrecognised_charge_is_not_marked_as_the_houses(self):
        orders = [order("EVT", EVENT_TM, sc_cents=20400, extra_charges=[
            {"type": "CUSTOM", "name": "Room Hire", "percentage": None,
             "applied_money": money(5000)}])]
        ev = extract_event_money(orders, self.PAYMENTS, EVENT_TM, TZ, {}, self.HOUSE)
        assert ev["other_charges"][0]["house"] is False

    def test_the_daily_gratuity_honours_the_same_list(self):
        """A catering admin fee can land on an ordinary ticket, not just an
        event one."""
        orders = [order("PDR", "TM_SERVER", sc_cents=4100, extra_charges=[
            {"type": "AUTO_GRATUITY", "name": "Event Administrative Fee",
             "percentage": "3", "applied_money": money(1200)}])]
        grat = extract_auto_gratuity(orders, {}, self.PAYMENTS, house_names=self.HOUSE)
        assert grat["auto_gratuity_cents"] == 4100
