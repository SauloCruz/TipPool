"""Pure extractor tests over synthetic Square API payloads (no network)."""

from datetime import date
from decimal import Decimal

from engine import TippableWindow

from app.square_extract import (
    build_catalog_lookup,
    extract_auto_gratuity,
    extract_credit_tips,
    extract_food_sales,
    extract_timecards,
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


def timecard(tmid, start, end, declared=0, breaks=None):
    tc = {"team_member_id": tmid, "start_at": start, "declared_cash_tip_money": money(declared)}
    if end:
        tc["end_at"] = end
    if breaks:
        tc["breaks"] = breaks
    return tc


def run_extract(timecards):
    # 0.01 = the app default since the 2026-07-05 owner ruling (exact minutes)
    return extract_timecards(timecards, EMPS, DAY, WINDOWS, TZ, Decimal("0.01"))


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

    def test_manager_timecard_fully_ignored(self):
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z", declared=1000),
            timecard("TM_BOSS", "2026-07-04T00:00:00Z", "2026-07-04T06:00:00Z", declared=9900),
        ])
        assert out["cash_tips_cents"] == 1000  # boss's declaration excluded
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
        assert set(blocking[0]["blocks"]) == {"foh_hours", "boh_worked", "cash_tips_cents"}

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
        # 5:00 PM - 9:07 PM = 4h07m = 4.1166... -> 4.12 (not 4.00)
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T04:07:00Z"),
        ])
        assert out["foh_hours"] == {"1": 4.12}

    def test_second_precision_not_rounded_before_calc(self):
        # 5:00:30 PM - 11:00:00 PM = 5h59m30s = 5.9917 -> 5.99
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:30Z", "2026-07-04T06:00:00Z"),
        ])
        assert out["foh_hours"] == {"1": 5.99}

    def test_overnight_shift_clipped_at_midnight_exact(self):
        # 6:23 PM - 1:30 AM: tippable 6:23 PM - midnight = 5h37m = 5.62
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T01:23:00Z", "2026-07-04T08:30:00Z"),
        ])
        assert out["foh_hours"] == {"1": 5.62}

    def test_double_shift_hours_summed(self):
        out = run_extract([
            timecard("TM_BREE", "2026-07-04T00:00:00Z", "2026-07-04T02:00:00Z"),
            timecard("TM_BREE", "2026-07-04T04:00:00Z", "2026-07-04T07:00:00Z"),
        ])
        assert out["foh_hours"] == {"1": 5.0}
