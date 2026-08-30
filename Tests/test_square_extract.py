"""Pure extractor tests over synthetic Square API payloads (no network)."""

from datetime import date
from decimal import Decimal
from fractions import Fraction

from engine import TippableWindow

from app.square_extract import (
    build_catalog_lookup,
    extract_auto_gratuity,
    extract_credit_tips,
    extract_event_items,
    extract_event_tips,
    extract_food_sales,
    extract_timecards,
    extract_timecards_poq,
)

TZ = "America/Los_Angeles"
WINDOWS = {wd: TippableWindow() for wd in range(7)}
# 2026-07-03 is a Friday
DAY = date(2026, 7, 3)


def money(cents):
    return {"amount": cents, "currency": "USD"}


# ---------- catalog / food sales ----------

CATALOG_BATCH = {
    "objects": [
        {"id": "VAR_BURGER", "type": "ITEM_VARIATION",
         "item_variation_data": {"item_id": "ITEM_BURGER"}},
        {"id": "VAR_BEER", "type": "ITEM_VARIATION",
         "item_variation_data": {"item_id": "ITEM_BEER"}},
        {"id": "VAR_MYSTERY", "type": "ITEM_VARIATION",
         "item_variation_data": {"item_id": "ITEM_MYSTERY"}},
    ],
    "related_objects": [
        {"id": "ITEM_BURGER", "type": "ITEM",
         "item_data": {"name": "Tavern Burger",
                       "reporting_category": {"id": "CAT_FOOD"}}},
        {"id": "ITEM_BEER", "type": "ITEM",
         "item_data": {"name": "IPA", "categories": [{"id": "CAT_BEER"}]}},
        {"id": "ITEM_MYSTERY", "type": "ITEM",
         "item_data": {"name": "Mystery Special",
                       "reporting_category": {"id": "CAT_NEW"}}},
        {"id": "CAT_FOOD", "type": "CATEGORY", "category_data": {"name": "Kitchen"}},
        {"id": "CAT_BEER", "type": "CATEGORY", "category_data": {"name": "Draft Beer"}},
        {"id": "CAT_NEW", "type": "CATEGORY", "category_data": {"name": "Specials"}},
    ],
}

CATEGORY_MAP = {
    "CAT_FOOD": {"name": "Kitchen", "group": "FOOD"},
    "CAT_BEER": {"name": "Draft Beer", "group": "ALCOHOL"},
    # CAT_NEW deliberately unmapped
}


def order(line_items, service_charges=None, oid="O1"):
    return {"id": oid, "line_items": line_items,
            "service_charges": service_charges or []}


def li(var_id, gross):
    return {"catalog_object_id": var_id, "gross_sales_money": money(gross)}


class TestFoodSales:
    def test_food_counted_alcohol_not(self):
        lookup = build_catalog_lookup(CATALOG_BATCH)
        out = extract_food_sales(
            [order([li("VAR_BURGER", 1800), li("VAR_BEER", 900),
                    li("VAR_BURGER", 1650)])],
            lookup, CATEGORY_MAP)
        assert out["food_sales_cents"] == 3450
        assert out["issues"] == []

    def test_unmapped_category_blocks(self):
        lookup = build_catalog_lookup(CATALOG_BATCH)
        out = extract_food_sales(
            [order([li("VAR_BURGER", 1800), li("VAR_MYSTERY", 1200)])],
            lookup, CATEGORY_MAP)
        blocking = [i for i in out["issues"] if i["severity"] == "blocking"]
        assert len(blocking) == 1
        assert blocking[0]["code"] == "unmapped_category"
        assert blocking[0]["detail"] == {"CAT_NEW": "Specials"}
        assert "food_sales_cents" in blocking[0]["blocks"]
        # mapped food still totalled so the manager can see it
        assert out["food_sales_cents"] == 1800

    def test_custom_amount_line_is_warning_not_counted(self):
        lookup = build_catalog_lookup(CATALOG_BATCH)
        out = extract_food_sales(
            [order([{"gross_sales_money": money(2500)}])], lookup, CATEGORY_MAP)
        assert out["food_sales_cents"] == 0
        assert out["issues"][0]["code"] == "uncataloged_line_items"
        assert out["issues"][0]["severity"] == "warning"


# ---------- credit tips ----------

def payment(pid, tip, total, refunded=0, status="COMPLETED", card=True):
    p = {"id": pid, "status": status, "tip_money": money(tip),
         "total_money": money(total)}
    if refunded:
        p["refunded_money"] = money(refunded)
    if card:
        p["card_details"] = {"status": "CAPTURED"}
    return p


class TestCreditTips:
    def test_card_tips_summed_cash_ignored(self):
        out = extract_credit_tips([
            payment("P1", 500, 5500),
            payment("P2", 725, 8000),
            payment("P3", 300, 3300, card=False),  # cash tender
        ])
        assert out["credit_tips_cents"] == 1225

    def test_incomplete_payments_ignored(self):
        out = extract_credit_tips([payment("P1", 500, 5500, status="FAILED")])
        assert out["credit_tips_cents"] == 0

    def test_full_refund_removes_tip(self):
        out = extract_credit_tips([payment("P1", 500, 5500, refunded=5500)])
        assert out["credit_tips_cents"] == 0

    def test_partial_refund_eats_nontip_first(self):
        # refund $52 of a $55 payment with $5 tip: tip loses only $2
        out = extract_credit_tips([payment("P1", 500, 5500, refunded=5200)])
        assert out["credit_tips_cents"] == 300


# ---------- auto gratuity ----------

class TestAutoGratuity:
    CFG = {"catalog_object_id": None, "name_contains": "gratuity"}

    def test_typed_auto_gratuity_matched_without_name(self):
        # real payload shape: catalog gratuity charges carry type + catalog id
        # but NO name, and total_money includes tax — applied_money is owed
        orders = [order([], [{
            "type": "AUTO_GRATUITY", "catalog_object_id": "3QWQ2YPHUCV7",
            "applied_money": money(2260), "total_money": money(2494),
            "total_tax_money": money(234), "percentage": "20",
        }])]
        out = extract_auto_gratuity(orders, self.CFG)
        assert out["auto_gratuity_cents"] == 2260  # pre-tax, never 2494

    def test_tax_stripped_when_applied_money_missing(self):
        orders = [order([], [{"type": "AUTO_GRATUITY",
                              "total_money": money(2494),
                              "total_tax_money": money(234)}])]
        out = extract_auto_gratuity(orders, self.CFG)
        assert out["auto_gratuity_cents"] == 2260

    def test_custom_charge_matched_by_name(self):
        orders = [order([], [{"name": "Auto Gratuity 20%", "type": "CUSTOM",
                              "applied_money": money(10800)},
                             {"name": "Delivery Fee", "type": "CUSTOM",
                              "applied_money": money(500)}])]
        out = extract_auto_gratuity(orders, self.CFG)
        assert out["auto_gratuity_cents"] == 10800

    def test_catalog_id_match(self):
        orders = [order([], [{"name": "whatever", "catalog_object_id": "SC_GRAT",
                              "type": "CUSTOM", "applied_money": money(4200)},
                             {"name": "Delivery Fee", "type": "CUSTOM",
                              "applied_money": money(999)}])]
        out = extract_auto_gratuity(orders, {"catalog_object_id": "SC_GRAT",
                                             "name_contains": ""})
        assert out["auto_gratuity_cents"] == 4200

    def test_unrelated_charges_ignored(self):
        orders = [order([], [{"name": "Delivery Fee", "type": "CUSTOM",
                              "applied_money": money(500)}])]
        out = extract_auto_gratuity(orders, self.CFG)
        assert out["auto_gratuity_cents"] == 0


# ---------- timecards ----------

EMPS = {
    "TM_BREE": {"id": 1, "display_name": "Bree", "pool_role": "FOH"},
    "TM_KELLY": {"id": 2, "display_name": "Kelly", "pool_role": "FOH"},
    "TM_BENITO": {"id": 4, "display_name": "Benito", "pool_role": "BOH"},
    "TM_BOSS": {"id": 9, "display_name": "Saulo", "pool_role": "EXCLUDED"},
}


# Since 2026-08-29 the SHIFT decides the pool role, so every timecard carries
# the Square job it was clocked in under. These mirror the live Tavern Law
# jobs; DOOR is FOH at tl_door_weight.
JOB_ROLES = {"Bartender": "FOH", "Server": "FOH", "Host": "DOOR",
             "Kitchen Staff": "BOH", "Bar Manager": "EXCLUDED",
             "Manager": "EXCLUDED", "Owner": "EXCLUDED"}
DEFAULT_JOB = {"TM_BREE": "Bartender", "TM_KELLY": "Bartender",
               "TM_BENITO": "Kitchen Staff", "TM_BOSS": "Owner"}


def timecard(tmid, start, end, declared=0, breaks=None, job=None):
    tc = {"team_member_id": tmid, "start_at": start,
          "declared_cash_tip_money": money(declared),
          "wage": {"title": job or DEFAULT_JOB.get(tmid, "Bartender")}}
    if end:
        tc["end_at"] = end
    if breaks:
        tc["breaks"] = breaks
    return tc


def run_extract(timecards, job_roles=None, door_weight=Fraction(1, 2)):
    # 0.05 = the app default since the 2026-07-29 owner ruling: credited
    # hours step in 0.05 and always round UP (supersedes the 0.01 ruling)
    return extract_timecards(timecards, EMPS, DAY, WINDOWS, TZ, Decimal("0.05"),
                             job_roles=job_roles or JOB_ROLES,
                             door_weight=door_weight)


class TestTimecards:
    def test_one_pull_three_inputs(self):
        # Bree: 3 PM prep in, 12:40 AM out (UTC-7 in July) -> 7.00 tippable
        out = run_extract([
            timecard("TM_BREE", "2026-07-03T22:00:00Z", "2026-07-04T07:40:00Z", declared=2500),
            timecard("TM_BENITO", "2026-07-03T18:00:00Z", "2026-07-04T05:00:00Z", declared=1500),
        ])
        assert out["foh_hours"] == {"1": 7.0}
        assert out["boh_worked"] == [4]
        assert out["cash_tips_cents"] == 4000
        assert out["issues"] == []

    def test_excluded_job_earns_nothing_but_its_cash_is_pooled(self):
        # Owner 2026-08-29: "my hours don't count, but any tip I capture goes
        # into the pool as I cannot retain them." Supersedes the older rule
        # that dropped a manager's timecard whole, declared cash included.
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z", declared=1000),
            timecard("TM_BOSS", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z", declared=9900),
        ])
        assert out["cash_tips_cents"] == 10900
        assert "9" not in out["foh_hours"] and 9 not in out["boh_worked"]

    def test_unpaid_break_deducted_paid_not(self):
        out = run_extract([
            # 5 PM - 11 PM with 30m unpaid + 15m paid break inside the window
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z",
                     breaks=[
                         {"start_at": "2026-07-04T02:00:00Z", "end_at": "2026-07-04T02:30:00Z",
                          "is_paid": False},
                         {"start_at": "2026-07-04T04:00:00Z", "end_at": "2026-07-04T04:15:00Z",
                          "is_paid": True},
                     ]),
        ])
        assert out["foh_hours"] == {"1": 5.5}

    def test_missing_clockout_warns_and_skips_hours(self):
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", None, declared=500),
            timecard("TM_KELLY", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z"),
        ])
        assert "1" not in out["foh_hours"]
        assert out["foh_hours"]["2"] == 6.0
        codes = {i["code"]: i for i in out["issues"]}
        assert codes["missing_clockout"]["severity"] == "warning"
        assert codes["missing_clockout"]["detail"] == ["Bree"]
        # declared tips still counted even without a clock-out
        assert out["cash_tips_cents"] == 500

    def test_unmapped_team_member_blocks_labor_fields(self):
        out = run_extract([
            timecard("TM_UNKNOWN", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z"),
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z"),
        ])
        blocking = [i for i in out["issues"] if i["severity"] == "blocking"]
        assert blocking[0]["code"] == "unmapped_team_member"
        assert blocking[0]["detail"] == ["TM_UNKNOWN"]
        assert set(blocking[0]["blocks"]) == {"foh_hours", "boh_worked",
                                              "cash_tips_cents", "foh_role_weights"}

    def test_all_zero_declarations_flagged(self):
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z", declared=0),
            timecard("TM_BENITO", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z", declared=0),
        ])
        assert any(i["code"] == "all_cash_tips_zero" for i in out["issues"])

    def test_exact_minutes_no_quarter_rounding(self):
        """Owner example: 5:09 PM in, 12:40 AM out. Full shift = 7h31m = 7.52
        as Square displays; tippable = 5:09 PM - midnight = 6h51m = 6.85.
        Must NOT be 6.75 (quarter-rounded) or 7.52 (unclipped)."""
        # DAY is Fri 2026-07-03; PDT = UTC-7
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:09:00Z", "2026-07-04T07:40:00Z"),
        ])
        assert out["foh_hours"] == {"1": 6.85}
        card = out["timecards"][0]
        assert card["raw_hours"] == 7.52
        assert card["tippable_hours"] == 6.85

    def test_same_day_partial_minutes(self):
        # 5:00 PM - 9:07 PM = 4h07m = 4.1166... -> rounds UP to 4.15
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T04:07:00Z"),
        ])
        assert out["foh_hours"] == {"1": 4.15}

    def test_second_precision_not_rounded_before_calc(self):
        # 5:00:30 PM - 11:00:00 PM = 5h59m30s = 5.9917 -> rounds UP to 6.00.
        # Seconds still reach the calc: truncating them first would give 5.95.
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:30Z", "2026-07-04T06:00:00Z"),
        ])
        assert out["foh_hours"] == {"1": 6.00}

    def test_overnight_shift_clipped_at_midnight_exact(self):
        # 6:23 PM - 1:30 AM: tippable 6:23 PM - midnight = 5h37m -> 5.65
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T01:23:00Z", "2026-07-04T08:30:00Z"),
        ])
        assert out["foh_hours"] == {"1": 5.65}

    def test_double_shift_hours_summed(self):
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T02:00:00Z"),
            timecard("TM_BREE", "2026-07-04T04:00:00Z", "2026-07-04T07:00:00Z"),
        ])
        assert out["foh_hours"] == {"1": 5.0}


class TestInvalidTimecardInterval:
    """Regression: 2026-07-25 crashed the whole pull with a bare 500 because
    one bartender double-punched — clocked in and out in the same minute, so
    clock_out was not after clock_in and clip_timecard raised. One bad punch
    must never fail a day's pull."""

    def test_zero_length_punch_does_not_raise(self):
        out = run_extract([
            # the real shape from 2026-07-25: same minute in and out
            timecard("TM_BREE", "2026-07-04T08:28:00Z", "2026-07-04T08:28:00Z"),
            timecard("TM_KELLY", "2026-07-04T00:00:00Z", "2026-07-04T07:00:00Z"),
        ])
        assert out["foh_hours"] == {"2": 7.0}          # Bree contributes nothing
        codes = {i["code"] for i in out["issues"]}
        assert "invalid_timecard" in codes
        issue = next(i for i in out["issues"] if i["code"] == "invalid_timecard")
        assert issue["severity"] == "warning"          # 0 hours is unambiguous
        assert any("Bree" in d for d in issue["detail"])

    def test_backwards_punch_does_not_raise(self):
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T04:00:00Z", "2026-07-04T02:00:00Z"),
        ])
        assert out["foh_hours"] == {}
        assert "invalid_timecard" in {i["code"] for i in out["issues"]}

    def test_good_punches_on_the_same_person_still_count(self):
        """The bartender also had a valid 1-minute punch right after."""
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T08:28:00Z", "2026-07-04T08:28:00Z"),
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T07:00:00Z"),
        ])
        assert out["foh_hours"] == {"1": 7.0}
        assert "invalid_timecard" in {i["code"] for i in out["issues"]}

    def test_declared_cash_still_collected_from_a_bad_punch(self):
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T08:28:00Z", "2026-07-04T08:28:00Z",
                     declared=1500),
        ])
        assert out["cash_tips_cents"] == 1500


class TestRefundedServiceCharges:
    """Money handed back is not owed to staff. A refund eats the non-charge
    part of the check first, so a full refund returns the whole gratuity and a
    small partial refund returns none — matching Square's Net Service Charges.
    (2026-08-14: this was over-distributing $55.30 in one pay period.)"""

    CFG = {"catalog_object_id": None, "name_contains": "gratuity"}

    def sc_order(self, oid, cents):
        return {"id": oid, "service_charges": [
            {"type": "AUTO_GRATUITY", "applied_money": money(cents)}]}

    def payment(self, oid, total, refunded):
        return {"order_id": oid, "total_money": money(total),
                "refunded_money": money(refunded)}

    def test_fully_refunded_check_returns_its_whole_gratuity(self):
        out = extract_auto_gratuity([self.sc_order("O1", 5530)], self.CFG,
                                    [self.payment("O1", 36680, 36680)])
        assert out["auto_gratuity_cents"] == 0
        assert out["refunded_gratuity_cents"] == 5530

    def test_small_partial_refund_leaves_the_gratuity_alone(self):
        """The refund is absorbed by the food/drink portion first."""
        out = extract_auto_gratuity([self.sc_order("O2", 6560)], self.CFG,
                                    [self.payment("O2", 51512, 6000)])
        assert out["auto_gratuity_cents"] == 6560
        assert out["refunded_gratuity_cents"] == 0

    def test_partial_refund_bigger_than_the_food_eats_into_it(self):
        # check 100.00 of which 20.00 is gratuity; 90.00 refunded
        # -> 10.00 of the gratuity comes back, 10.00 stays
        out = extract_auto_gratuity([self.sc_order("O3", 2000)], self.CFG,
                                    [self.payment("O3", 10000, 9000)])
        assert out["auto_gratuity_cents"] == 1000
        assert out["refunded_gratuity_cents"] == 1000

    def test_no_refunds_behaves_exactly_as_before(self):
        out = extract_auto_gratuity([self.sc_order("O4", 4200)], self.CFG,
                                    [self.payment("O4", 20000, 0)])
        assert out["auto_gratuity_cents"] == 4200
        assert out["refunded_gratuity_cents"] == 0

    def test_payments_argument_is_optional(self):
        """Callers that don't pass payments keep the old behaviour."""
        out = extract_auto_gratuity([self.sc_order("O5", 999)], self.CFG)
        assert out["auto_gratuity_cents"] == 999

    def test_real_period_shape(self):
        """The 2026-08-01..14 case: one fully refunded check carrying 55.30
        alongside a partially refunded one carrying 65.60."""
        out = extract_auto_gratuity(
            [self.sc_order("A", 5530), self.sc_order("B", 6560)], self.CFG,
            [self.payment("A", 36680, 36680), self.payment("B", 51512, 6000)])
        assert out["auto_gratuity_cents"] == 6560     # only B survives
        assert out["refunded_gratuity_cents"] == 5530


class TestExcludedStaffCashStillPools:
    """Owner 2026-08-15: tips an excluded person captures still go into the
    pool — they simply receive none of it. Poquitos differs from Tavern Law
    here, where manager timecards are ignored outright."""

    def test_owner_declared_cash_is_pooled(self):
        def tc(tm, title, declared):
            return {"team_member_id": tm,
                    "start_at": "2026-08-07T18:00:00-07:00",
                    "end_at": "2026-08-08T02:00:00-07:00",
                    "wage": {"title": title},
                    "declared_cash_tip_money": money(declared)}
        emps = {"T1": {"id": 1, "display_name": "Owner", "pool_role": "EXCLUDED"},
                "T2": {"id": 2, "display_name": "Srv", "pool_role": "FOH"}}
        out = extract_timecards_poq(
            [tc("T1", "Owner", 14300), tc("T2", "Server", 1000)],
            emps, "America/Los_Angeles", Decimal("0"),
            {"Owner": "OWNER", "Server": "SERVER"})
        assert out["cash_tips_cents"] == 15300      # both, not just the server
        roles = {s["name"]: s["role"] for s in out["shifts"]}
        assert roles["Owner"] == "OWNER"            # shift kept, earns nothing


class TestJobDrivenRoles:
    """The shift decides the pool role, not the person (owner 2026-08-29).
    Tavern Law staff hold two Square jobs — a bartender who also manages, a
    server who also hosts — so the job clocked in under is what it is worth.
    """

    def test_door_job_earns_half_credit(self):
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z",
                     job="Host"),
        ])
        # hours reported as worked, weight carries the halving
        assert out["foh_hours"] == {"1": 6.0}
        assert out["foh_role_weights"] == {"1": "1/2"}

    def test_floor_job_carries_no_weight_at_all(self):
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z"),
        ])
        assert out["foh_role_weights"] == {}

    def test_split_floor_and_door_night_blends(self):
        # 4 h on the floor at full credit + 2 h on the door at half = 5 of 6
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T04:00:00Z"),
            timecard("TM_BREE", "2026-07-04T04:00:00Z", "2026-07-04T06:00:00Z",
                     job="Host"),
        ])
        assert out["foh_hours"] == {"1": 6.0}
        assert out["foh_role_weights"] == {"1": "5/6"}

    def test_door_weight_is_configurable(self):
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z",
                     job="Host"),
        ], door_weight=Fraction(3, 4))
        assert out["foh_role_weights"] == {"1": "3/4"}

    def test_same_person_manager_night_and_bartender_night(self):
        # Jacob Ruley's real August: Bar Manager some nights, Bartender others.
        # Only the bartending hours reach the pool.
        emps = {"TM_JACOB": {"id": 7, "display_name": "Jacob", "pool_role": "FOH"}}
        mgr = extract_timecards(
            [timecard("TM_JACOB", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z",
                      declared=1200, job="Bar Manager")],
            emps, DAY, WINDOWS, TZ, Decimal("0.05"), job_roles=JOB_ROLES)
        assert mgr["foh_hours"] == {} and mgr["boh_worked"] == []
        assert mgr["cash_tips_cents"] == 1200          # collected, so pooled
        bar = extract_timecards(
            [timecard("TM_JACOB", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z",
                      job="Bartender")],
            emps, DAY, WINDOWS, TZ, Decimal("0.05"), job_roles=JOB_ROLES)
        assert bar["foh_hours"] == {"7": 6.0}

    def test_unmapped_job_blocks_the_day(self):
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z",
                     job="Sommelier"),
        ])
        blocking = [i for i in out["issues"] if i["severity"] == "blocking"]
        assert blocking[0]["code"] == "unmapped_job_title"
        assert blocking[0]["detail"] == ["Sommelier"]
        assert out["foh_hours"] == {}

    def test_job_contradicting_the_staff_record_warns(self):
        out = run_extract([
            timecard("TM_BENITO", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z",
                     job="Bartender"),
        ])
        warn = [i for i in out["issues"] if i["code"] == "job_role_mismatch"]
        assert warn and "Benito" in warn[0]["detail"][0]
        assert out["foh_hours"] == {"4": 6.0}   # the job still wins


# ---------- private events (Tavern Law) ----------

EVENT_CATS = {"CAT_EVENT": {"name": "Events & Catering", "group": "EVENT"},
              "CAT_FOOD": {"name": "Food", "group": "FOOD"}}
EVENT_LOOKUP = {
    "V_EVFOOD": {"category_id": "CAT_EVENT", "category_name": "Events & Catering",
                 "item_name": "Event Food Packages"},
    "V_EVBEV": {"category_id": "CAT_EVENT", "category_name": "Events & Catering",
                "item_name": "Event Beverage Package"},
    "V_EVDEP": {"category_id": "CAT_EVENT", "category_name": "Events & Catering",
                "item_name": "Event Deposit"},
    "V_BURGER": {"category_id": "CAT_FOOD", "category_name": "Food",
                 "item_name": "Tavern Burger"},
}
EVENT_CFG = {"food_contains": "food", "deposit_contains": "deposit"}


def _li(var, cents, uid="u1", note=None):
    return {"uid": uid, "catalog_object_id": var, "name": "x",
            "gross_sales_money": money(cents), "note": note}


class TestEventItems:
    def test_every_event_food_line_counts(self):
        # 8/22 rang $440 and $75 on two tickets; 8/07's second line was missed
        # by hand and short-changed the kitchen.
        orders = [{"id": "O1", "line_items": [_li("V_EVFOOD", 44000)]},
                  {"id": "O2", "line_items": [_li("V_EVFOOD", 7500)]}]
        out = extract_event_items(orders, EVENT_LOOKUP, EVENT_CATS, EVENT_CFG)
        assert out["event_food_cents"] == 51500

    def test_beverage_and_room_held_out_and_reported(self):
        orders = [{"id": "O1", "line_items": [_li("V_EVBEV", 157500)]}]
        out = extract_event_items(orders, EVENT_LOOKUP, EVENT_CATS, EVENT_CFG)
        assert out["event_food_cents"] == 0
        assert out["other_cents"] == 157500
        assert out["other_lines"][0]["item"] == "Event Beverage Package"

    def test_event_lines_never_reach_food_sales(self):
        orders = [{"id": "O1", "line_items": [_li("V_EVFOOD", 44000),
                                              _li("V_BURGER", 2100, uid="u2")]}]
        food = extract_food_sales(orders, EVENT_LOOKUP, EVENT_CATS)
        assert food["food_sales_cents"] == 2100
        assert not [i for i in food["issues"] if i["severity"] == "blocking"]

    def test_deposit_is_offered_not_counted(self):
        orders = [{"id": "O1", "created_at": "2026-08-16T04:27:36Z",
                   "tenders": [{"id": "3p1baGg6AvlBOMpDR6zL8orELraZY"}],
                   "line_items": [_li("V_EVDEP", 56924, note="Deposit for 8/22")]}]
        out = extract_event_items(orders, EVENT_LOOKUP, EVENT_CATS, EVENT_CFG)
        assert out["event_food_cents"] == 0 and out["other_cents"] == 0
        dep, = out["deposits"]
        assert dep["gross_cents"] == 56924
        assert dep["deposit_id"] == "O1:u1"
        assert dep["receipt"] == "3p1b"
        assert dep["note"] == "Deposit for 8/22"

    def test_deposit_note_falls_back_to_the_tender(self):
        orders = [{"id": "O1", "created_at": "2026-08-16T04:27:36Z",
                   "tenders": [{"id": "T1", "note": "Jacob O'Brien Deposit for 8/22"}],
                   "line_items": [_li("V_EVDEP", 56924)]}]
        out = extract_event_items(orders, EVENT_LOOKUP, EVENT_CATS, EVENT_CFG)
        assert out["deposits"][0]["note"] == "Jacob O'Brien Deposit for 8/22"


class TestEventTips:
    CFG = {"catalog_object_id": None, "name_contains": "gratuity"}

    def test_gratuity_on_an_event_ticket_is_event_money(self):
        # the real 8/22: a $75 event-food ticket carrying $55.80 of gratuity
        orders = [{"id": "O2", "line_items": [_li("V_EVFOOD", 7500)],
                   "service_charges": [{"type": "AUTO_GRATUITY",
                                        "applied_money": money(5580)}]}]
        pays = [{"order_id": "O2", "status": "COMPLETED", "card_details": {},
                 "tip_money": money(0), "total_money": money(37012)}]
        out = extract_event_tips(orders, pays, ["O2"], self.CFG)
        assert out["event_tips_cents"] == 5580

    def test_card_tip_on_an_event_ticket_is_event_money(self):
        orders = [{"id": "O2", "line_items": [_li("V_EVFOOD", 7500)]}]
        pays = [{"order_id": "O2", "status": "COMPLETED", "card_details": {},
                 "tip_money": money(9000), "total_money": money(50000)}]
        out = extract_event_tips(orders, pays, ["O2"], self.CFG)
        assert out["event_tips_cents"] == 9000

    def test_ordinary_ticket_untouched(self):
        orders = [{"id": "O3", "line_items": [_li("V_BURGER", 2100)],
                   "service_charges": [{"type": "AUTO_GRATUITY",
                                        "applied_money": money(2660)}]}]
        out = extract_event_tips(orders, [], ["O2"], self.CFG)
        assert out["event_tips_cents"] == 0

    def test_fully_refunded_event_ticket_returns_its_gratuity(self):
        orders = [{"id": "O2", "line_items": [_li("V_EVFOOD", 7500)],
                   "service_charges": [{"type": "AUTO_GRATUITY",
                                        "applied_money": money(5580)}]}]
        pays = [{"order_id": "O2", "status": "COMPLETED", "card_details": {},
                 "tip_money": money(0), "total_money": money(37012),
                 "refunded_money": money(37012)}]
        out = extract_event_tips(orders, pays, ["O2"], self.CFG)
        assert out["event_tips_cents"] == 0


class TestServiceChargesAreNeverSilentlyDropped:
    """A charge that matches nothing is money nobody has accounted for.
    Tavern Law rang $59.80 on 2026-08-05 as a CUSTOM charge named "Service
    Charge": it matched neither the AUTO_GRATUITY type nor "gratuity", so it
    vanished from the day while the spreadsheet paid it out."""

    CFG = {"catalog_object_id": None, "name_contains": "gratuity"}

    def test_unmatched_charge_is_reported(self):
        orders = [{"id": "O1", "service_charges": [
            {"name": "Service Charge", "type": "CUSTOM",
             "applied_money": money(5980)}]}]
        out = extract_auto_gratuity(orders, self.CFG)
        assert out["auto_gratuity_cents"] == 0
        warn, = out["issues"]
        assert warn["code"] == "unmatched_service_charge"
        assert warn["detail"]["cents"] == 5980
        assert warn["detail"]["names"] == ["Service Charge"]

    def test_a_known_house_charge_is_not_a_warning(self):
        orders = [{"id": "O1", "service_charges": [
            {"name": "Administrative Fee", "type": "CUSTOM",
             "applied_money": money(3000)}]}]
        out = extract_auto_gratuity(orders, self.CFG,
                                    house_names=["administrative fee"])
        assert out["auto_gratuity_cents"] == 0 and out["issues"] == []

    def test_name_match_accepts_several_names(self):
        orders = [{"id": "O1", "service_charges": [
            {"name": "Service Charge", "type": "CUSTOM",
             "applied_money": money(5980)}]}]
        cfg = {"catalog_object_id": None, "name_contains": "gratuity, service charge"}
        out = extract_auto_gratuity(orders, cfg)
        assert out["auto_gratuity_cents"] == 5980 and out["issues"] == []

    def test_name_match_accepts_a_list(self):
        orders = [{"id": "O1", "service_charges": [
            {"name": "Service Charge", "type": "CUSTOM",
             "applied_money": money(5980)}]}]
        cfg = {"catalog_object_id": None, "name_contains": ["gratuity", "service charge"]}
        assert extract_auto_gratuity(orders, cfg)["auto_gratuity_cents"] == 5980


class TestHoursRoundOncePerDay:
    """A split punch must not out-earn an unbroken shift. Jacob Ruley worked
    17:00-22:46 then 22:46-00:36 on 2026-08-13 — exactly 7.00 tippable hours,
    but rounding each timecard first read 5.80 + 1.25 = 7.05."""

    def test_split_punch_matches_the_unbroken_shift(self):
        split = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T05:46:00Z"),
            timecard("TM_BREE", "2026-07-04T05:46:00Z", "2026-07-04T07:36:00Z"),
        ])
        whole = run_extract([
            timecard("TM_KELLY", "2026-07-04T00:00:00Z", "2026-07-04T07:36:00Z"),
        ])
        assert split["foh_hours"]["1"] == whole["foh_hours"]["2"] == 7.0

    def test_the_day_total_still_rounds_up(self):
        # 17:00 -> 23:47 = 6.7833 h -> 6.80
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T06:47:00Z"),
        ])
        assert out["foh_hours"] == {"1": 6.8}


class TestShortKitchenShift:
    """Any kitchen timecard puts someone on the roster (owner rule), so a
    stray one-minute punch splits the allocation one more way. Jose Medina's
    2026-08-01 "shift" was 17:00-17:01. The roster still counts him — only a
    manager may take someone off — but the day names the punch."""

    def test_one_minute_punch_is_flagged_but_still_counted(self):
        out = run_extract([
            timecard("TM_BENITO", "2026-07-04T00:00:00Z", "2026-07-04T00:01:00Z"),
        ])
        assert out["boh_worked"] == [4]
        warn, = [i for i in out["issues"] if i["code"] == "short_kitchen_shift"]
        assert warn["detail"] == ["Benito (1 min)"]

    def test_a_real_kitchen_shift_is_not_flagged(self):
        out = run_extract([
            timecard("TM_BENITO", "2026-07-03T21:00:00Z", "2026-07-04T06:00:00Z"),
        ])
        assert out["boh_worked"] == [4]
        assert not [i for i in out["issues"] if i["code"] == "short_kitchen_shift"]
