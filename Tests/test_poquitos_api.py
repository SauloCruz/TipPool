"""M6 integration: the Poquitos venue end to end — day inputs carrying
per-timecard roles, the 80/20 points split, the event pool, and a Square
pull that reads roles from wage.title."""

import os

import pytest
from fastapi.testclient import TestClient


def money(cents):
    return {"amount": cents, "currency": "USD"}


class FakePoqSquare:
    def __init__(self):
        self.payments, self.orders, self.timecards = [], [], []

    def list_payments(self, b, e): return self.payments
    def search_orders(self, b, e): return self.orders
    def search_timecards(self, b, e): return self.timecards
    def batch_retrieve_catalog(self, ids): return {"objects": [], "related_objects": []}
    def list_categories(self): return []
    def search_team_members(self): return []


@pytest.fixture(scope="module")
def fake():
    return FakePoqSquare()


@pytest.fixture(scope="module")
def client(tmp_path_factory, fake):
    data_dir = tmp_path_factory.mktemp("m6data")
    env = {
        "DATA_DIR": str(data_dir), "DB_PATH": str(data_dir / "m6.sqlite3"),
        "TIMEZONE": "America/Los_Angeles", "VENUE_NAME": "Tavern Law Test",
        "ADMIN_EMAIL": "owner@test.local", "ADMIN_PASSWORD": "super-secret-1",
        "SQUARE_ACCESS_TOKEN__POQUITOS": "poq-token",
        "SQUARE_LOCATION_ID__POQUITOS": "LOC_POQ",
        "NIGHTLY_SYNC": "0",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        from app.config import Settings
        from app.main import create_app
        app = create_app(Settings(env_file="/nonexistent"))
        app.state.square_client_factory = lambda slug=None: fake
        with TestClient(app) as c:
            c.post("/api/login", json={"email": "owner@test.local",
                                       "password": "super-secret-1"})
            yield c
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})


@pytest.fixture(scope="module")
def poq(client):
    v = {x["slug"]: x for x in client.get("/api/venues").json()}["poquitos"]
    return {"id": v["id"], "h": {"X-Venue-Id": str(v["id"])}}


@pytest.fixture(scope="module")
def staff(client, poq):
    ids = {}
    for name, tmid in [("Ana", "TM_ANA"), ("Ben", "TM_BEN"), ("Cid", "TM_CID"),
                       ("Dee", "TM_DEE"), ("Mgr", "TM_MGR")]:
        r = client.post("/api/employees", headers=poq["h"], json={
            "display_name": name, "pool_role": "FOH",
            "square_team_member_id": tmid})
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
    return ids


DAY = "2026-08-08"


class TestVenueExists:
    def test_seeded_with_the_points_model(self, client, poq):
        v = {x["slug"]: x for x in client.get("/api/venues").json()}["poquitos"]
        assert v["tip_model"] == "POINTS_HOURS"
        assert v["square_configured"]

    def test_settings_carry_the_policy(self, client, poq):
        s = client.get("/api/settings", headers=poq["h"]).json()
        assert s["poq_foh_pct"] == "80"
        assert s["poq_roles"]["BARTENDER"]["points"] == "1.25"
        assert s["poq_roles"]["SHIFT_MANAGER"]["side"] == "EXCLUDED"
        assert s["poq_job_roles"]["Runner"] == "FOOD_RUNNER"


class TestDailyPool:
    def test_shifts_drive_the_eighty_twenty_split(self, client, poq, staff):
        r = client.put(f"/api/days/{DAY}", headers=poq["h"], json={
            "credit_tips_cents": 100000,
            "shifts": [
                {"employee_id": staff["Ana"], "role": "BARTENDER", "hours": 8},
                {"employee_id": staff["Ben"], "role": "SERVER", "hours": 8},
                {"employee_id": staff["Cid"], "role": "LINE_COOK", "hours": 8},
            ]})
        assert r.status_code == 200, r.text
        out = r.json()["computed"]
        assert out["model"] == "POINTS_HOURS"
        assert out["totals"]["foh_pool_cents"] == 80000
        assert out["totals"]["boh_pool_cents"] == 20000
        rows = {p["name"]: p for p in out["people"]}
        assert rows["Ana"]["points"] == 10.0 and rows["Ana"]["tips_cents"] == 44444
        assert rows["Cid"]["tips_cents"] == 20000
        assert sum(p["tips_cents"] for p in out["people"]) == 100000

    def test_one_person_two_roles_in_a_day(self, client, poq, staff):
        out = client.put(f"/api/days/{DAY}", headers=poq["h"], json={
            "credit_tips_cents": 100000,
            "shifts": [
                {"employee_id": staff["Ana"], "role": "BARTENDER", "hours": 4},
                {"employee_id": staff["Ana"], "role": "HOST", "hours": 4},
                {"employee_id": staff["Ben"], "role": "SERVER", "hours": 7},
                {"employee_id": staff["Cid"], "role": "LINE_COOK", "hours": 8},
            ]}).json()["computed"]
        rows = {p["name"]: p for p in out["people"]}
        assert rows["Ana"]["points"] == 7.0     # 4x1.25 + 4x0.5
        assert rows["Ana"]["hours"] == 8.0

    def test_excluded_job_earns_nothing_and_does_not_dilute(self, client, poq, staff):
        out = client.put(f"/api/days/{DAY}", headers=poq["h"], json={
            "credit_tips_cents": 100000,
            "shifts": [
                {"employee_id": staff["Ben"], "role": "SERVER", "hours": 8},
                {"employee_id": staff["Mgr"], "role": "SHIFT_MANAGER", "hours": 9},
                {"employee_id": staff["Cid"], "role": "LINE_COOK", "hours": 8},
            ]}).json()["computed"]
        rows = {p["name"]: p for p in out["people"]}
        assert "Mgr" not in rows
        assert rows["Ben"]["tips_cents"] == 80000   # full FOH pool

    def test_unmapped_role_is_refused(self, client, poq, staff):
        r = client.put(f"/api/days/{DAY}", headers=poq["h"], json={
            "credit_tips_cents": 1000,
            "shifts": [{"employee_id": staff["Ana"], "role": "SOMMELIER", "hours": 5}]})
        assert r.status_code == 422
        assert "SOMMELIER" in r.text


class TestEventPool:
    def test_event_money_creates_an_event_block(self, client, poq, staff):
        out = client.put(f"/api/days/{DAY}", headers=poq["h"], json={
            "credit_tips_cents": 100000,
            "event_service_charge_cents": 200000,
            "shifts": [
                {"employee_id": staff["Ana"], "role": "EVENT_SERVER", "hours": 5},
                {"employee_id": staff["Ben"], "role": "SERVER", "hours": 8},
                {"employee_id": staff["Dee"], "role": "BUSSER", "hours": 6},
                {"employee_id": staff["Cid"], "role": "LINE_COOK", "hours": 8},
            ]}).json()["computed"]
        ev = out["event"]
        assert ev["pool_cents"] == 200000
        assert ev["foh_portion_cents"] == 160000
        assert ev["support_group_cents"]["BUSSER"] == 4800   # 3% of the FOH portion
        rows = {p["name"]: p for p in out["people"]}
        # event server is OUT of the daily pool but paid from the event
        assert rows["Ana"]["tips_cents"] == 0 and rows["Ana"]["event_cents"] > 0
        # busser is in BOTH
        assert rows["Dee"]["tips_cents"] > 0 and rows["Dee"]["event_cents"] == 4800

    def test_no_event_money_means_no_event_block(self, client, poq, staff):
        out = client.put(f"/api/days/{DAY}", headers=poq["h"], json={
            "credit_tips_cents": 5000,
            "shifts": [{"employee_id": staff["Ben"], "role": "SERVER", "hours": 5}],
        }).json()["computed"]
        assert "event" not in out


class TestSquarePull:
    def test_pull_reads_roles_from_the_job_title(self, client, poq, staff):
        fake = client.app.state.square_client_factory()
        fake.payments = [{"id": "P1", "status": "COMPLETED", "card_details": {},
                          "tip_money": money(50000), "total_money": money(200000)}]
        fake.timecards = [
            {"team_member_id": "TM_ANA", "start_at": "2026-08-08T18:00:00-07:00",
             "end_at": "2026-08-09T02:00:00-07:00", "wage": {"title": "Bartender"},
             "declared_cash_tip_money": money(1000)},
            {"team_member_id": "TM_CID", "start_at": "2026-08-08T16:00:00-07:00",
             "end_at": "2026-08-09T00:00:00-07:00", "wage": {"title": "Line Cook"},
             "declared_cash_tip_money": money(0)},
        ]
        r = client.post(f"/api/days/2026-08-09/pull", headers=poq["h"])
        assert r.status_code == 200, r.text
        body = r.json()
        roles = {s["role"] for s in body["inputs"]["shifts"]}
        assert roles == {"BARTENDER", "LINE_COOK"}
        assert body["inputs"]["credit_tips_cents"] == 50000
        assert body["inputs"]["cash_tips_cents"] == 1000

    def test_unknown_job_title_blocks_the_pull(self, client, poq, staff):
        fake = client.app.state.square_client_factory()
        fake.timecards = [
            {"team_member_id": "TM_ANA", "start_at": "2026-08-10T18:00:00-07:00",
             "end_at": "2026-08-11T02:00:00-07:00", "wage": {"title": "Sommelier"},
             "declared_cash_tip_money": money(0)}]
        body = client.post("/api/days/2026-08-10/pull", headers=poq["h"]).json()
        codes = {i["code"]: i for i in body["square"]["issues"]}
        assert codes["unmapped_job_title"]["severity"] == "blocking"
        assert "Sommelier" in codes["unmapped_job_title"]["detail"]
        # shifts must NOT be applied from a blocked pull
        assert body["inputs"]["shifts"] == []


class TestEventStaffWithoutEventMoney:
    """An event-role clock-in with no event money means that person is out of
    the daily pool with no event pool to pay them — silent zero. Flag it."""

    def test_flagged_when_event_money_is_missing(self, client, poq, staff):
        out = client.put("/api/days/2026-08-14", headers=poq["h"], json={
            "credit_tips_cents": 50000,
            "shifts": [
                {"employee_id": staff["Ana"], "role": "EVENT_SERVER", "hours": 6},
                {"employee_id": staff["Ben"], "role": "SERVER", "hours": 6},
            ]}).json()["computed"]
        assert out["flags"]["event_staff_without_event_money"] is True
        assert out["event_staff_unpaid"] == ["Ana"]

    def test_not_flagged_once_the_event_money_is_entered(self, client, poq, staff):
        out = client.put("/api/days/2026-08-14", headers=poq["h"], json={
            "credit_tips_cents": 50000, "event_service_charge_cents": 100000,
            "shifts": [
                {"employee_id": staff["Ana"], "role": "EVENT_SERVER", "hours": 6},
                {"employee_id": staff["Ben"], "role": "SERVER", "hours": 6},
            ]}).json()["computed"]
        assert out["flags"]["event_staff_without_event_money"] is False
        assert out["event_staff_unpaid"] == []

    def test_not_flagged_when_nobody_worked_an_event_role(self, client, poq, staff):
        out = client.put("/api/days/2026-08-14", headers=poq["h"], json={
            "credit_tips_cents": 50000,
            "shifts": [{"employee_id": staff["Ben"], "role": "SERVER", "hours": 6}],
        }).json()["computed"]
        assert out["flags"]["event_staff_without_event_money"] is False


class TestPeriodAndExport:
    """Semi-monthly period rollup and the payroll CSV. Points are carried
    through because they are the audit trail: tips / points is what one point
    was worth, so any row can be re-derived by hand."""

    P_DAY = "2026-08-20"

    def _finalize_a_day(self, client, poq, staff):
        r = client.put(f"/api/days/{self.P_DAY}", headers=poq["h"], json={
            "credit_tips_cents": 100000, "auto_gratuity_cents": 20000,
            "event_service_charge_cents": 100000,
            "shifts": [
                {"employee_id": staff["Ana"], "role": "BARTENDER", "hours": 8},
                {"employee_id": staff["Ben"], "role": "SERVER", "hours": 8},
                {"employee_id": staff["Dee"], "role": "BUSSER", "hours": 6},
                {"employee_id": staff["Cid"], "role": "LINE_COOK", "hours": 8},
            ]})
        assert r.status_code == 200, r.text
        assert client.post(f"/api/days/{self.P_DAY}/finalize",
                           headers=poq["h"]).status_code == 200

    def test_period_totals_use_the_eighty_twenty_shape(self, client, poq, staff):
        self._finalize_a_day(client, poq, staff)
        p = client.get(f"/api/periods/{self.P_DAY}/export", headers=poq["h"]).json()
        assert p["model"] == "POINTS_HOURS"
        assert p["totals"]["foh_pool_cents"] == 80000
        assert p["totals"]["boh_pool_cents"] == 20000
        assert p["totals"]["auto_gratuity_cents"] == 20000
        # semi-monthly, like Tavern Law
        assert (p["start"], p["end"]) == ("2026-08-16", "2026-08-31")

    def test_period_carries_hours_points_and_event_money(self, client, poq, staff):
        p = client.get(f"/api/periods/{self.P_DAY}/export", headers=poq["h"]).json()
        rows = {e["name"]: e for e in p["employees"]}
        assert rows["Ana"]["points"] == 10.0 and rows["Ana"]["hours"] == 8.0
        assert rows["Dee"]["event_cents"] > 0          # support tip-out
        assert all(e["days"] == 1 for e in p["employees"])

    def test_csv_columns_and_a_hand_checkable_row(self, client, poq, staff):
        r = client.get(f"/api/periods/{self.P_DAY}/export.csv", headers=poq["h"])
        assert r.status_code == 200
        lines = r.text.strip().splitlines()
        cols = lines[0].split(",")
        assert cols == ["Employee", "Tips (daily pool)", "Event Payout",
                        "Tips Total", "Auto Gratuity (wages)", "Take Home",
                        "Days Worked", "Hours", "Points"]
        # look columns up by NAME, so adding one never silently shifts a check
        ix = {c: i for i, c in enumerate(cols)}
        rows = {l.split(",")[0]: l.split(",") for l in lines[1:]}
        get = lambda who, col: float(rows[who][ix[col]])
        tips, points = get("Ana", "Tips (daily pool)"), get("Ana", "Points")
        # FOH pool $800 over 21 points (Ana 10 + Ben 8 + Dee 3) = $38.095/pt
        assert points == 10.0
        assert abs(tips - 380.95) < 0.01
        # the published points make the rate checkable by hand for everyone
        rate = tips / points
        for who in ("Ben", "Dee"):
            assert abs(get(who, "Tips (daily pool)")
                       - rate * get(who, "Points")) < 0.01, who
        # tips total = daily + event; take home adds the gratuity line
        assert abs(get("Ana", "Tips Total")
                   - (tips + get("Ana", "Event Payout"))) < 0.01
        assert abs(get("Ana", "Take Home")
                   - (get("Ana", "Tips Total")
                      + get("Ana", "Auto Gratuity (wages)"))) < 0.01

    def test_filename_is_venue_scoped(self, client, poq, staff):
        r = client.get(f"/api/periods/{self.P_DAY}/export.csv", headers=poq["h"])
        assert 'filename="tips_poquitos_' in r.headers["content-disposition"]


class TestCardFeeSetting:
    """The processing fee is a Setup input (owner 2026-08-13): it must be
    editable, must reduce only credit tips, and the rate used must be stored
    on the snapshot so a finalized day explains itself."""

    FEE_DAY = "2026-08-21"

    def test_default_is_zero_so_nothing_changes_silently(self, client, poq):
        s = client.get("/api/settings", headers=poq["h"]).json()
        assert s["poq_card_fee_pct"] == "0"

    def test_setting_the_rate_changes_the_pool(self, client, poq, staff):
        body = {"credit_tips_cents": 100000, "cash_tips_cents": 10000,
                "shifts": [
                    {"employee_id": staff["Ben"], "role": "SERVER", "hours": 8},
                    {"employee_id": staff["Cid"], "role": "LINE_COOK", "hours": 8}]}
        before = client.put(f"/api/days/{self.FEE_DAY}", headers=poq["h"],
                            json=body).json()["computed"]
        assert before["totals"]["total_tips_cents"] == 110000
        try:
            assert client.put("/api/settings", headers=poq["h"],
                              json={"poq_card_fee_pct": "3"}).status_code == 200
            after = client.put(f"/api/days/{self.FEE_DAY}", headers=poq["h"],
                               json=body).json()["computed"]
            t = after["totals"]
            assert t["credit_tips_gross_cents"] == 100000
            assert t["card_fee_cents"] == 3000        # card tips only
            assert t["total_tips_cents"] == 107000    # cash untouched
            assert after["card_fee_pct"] == "3"
            assert sum(p["tips_cents"] for p in after["people"]) == 107000
        finally:
            client.put("/api/settings", headers=poq["h"],
                       json={"poq_card_fee_pct": "0"})

    def test_finalized_day_keeps_the_rate_it_was_locked_with(self, client, poq, staff):
        client.put("/api/settings", headers=poq["h"], json={"poq_card_fee_pct": "5"})
        client.put("/api/days/2026-08-22", headers=poq["h"], json={
            "credit_tips_cents": 100000,
            "shifts": [{"employee_id": staff["Ben"], "role": "SERVER", "hours": 8}]})
        assert client.post("/api/days/2026-08-22/finalize",
                           headers=poq["h"]).status_code == 200
        # change the rate afterwards — the snapshot must not move
        client.put("/api/settings", headers=poq["h"], json={"poq_card_fee_pct": "0"})
        out = client.get("/api/days/2026-08-22", headers=poq["h"]).json()["computed"]
        assert out["card_fee_pct"] == "5"
        assert out["totals"]["card_fee_cents"] == 5000
        assert out["totals"]["total_tips_cents"] == 95000

    def test_bad_rates_rejected(self, client, poq):
        for bad in ("-1", "120", "abc"):
            r = client.put("/api/settings", headers=poq["h"],
                           json={"poq_card_fee_pct": bad})
            assert r.status_code == 422, bad


class TestPoquitosHoursAreNotRoundedUp:
    """Owner 2026-08-14: Poquitos keeps hours as Square reports them (2dp,
    nearest) instead of Tavern Law's round-up-to-0.05. Rounding up was what
    made these figures drift from the venue's previous tip-pool service."""

    def test_hours_match_square_to_the_hundredth(self, client, poq, staff):
        fake = client.app.state.square_client_factory()
        fake.payments = []
        fake.timecards = [
            # 3h22m = 3.3667 -> 3.37 (round-up-to-0.05 would give 3.40)
            {"team_member_id": "TM_ANA", "start_at": "2026-08-25T18:00:00-07:00",
             "end_at": "2026-08-25T21:22:00-07:00", "wage": {"title": "Shift Lead"},
             "declared_cash_tip_money": money(0)},
            # 10h43m = 10.7167 -> 10.72 (round-up would give 10.75)
            {"team_member_id": "TM_BEN", "start_at": "2026-08-25T15:00:00-07:00",
             "end_at": "2026-08-26T01:43:00-07:00", "wage": {"title": "Bartender"},
             "declared_cash_tip_money": money(0)},
        ]
        r = client.post("/api/days/2026-08-25/pull", headers=poq["h"])
        assert r.status_code == 200, r.text
        hours = sorted(s["hours"] for s in r.json()["inputs"]["shifts"])
        assert hours == [3.37, 10.72]

    def test_tavern_law_still_rounds_up(self):
        """The two venues must not share a rule — TL's ruling stands."""
        from decimal import Decimal
        from engine import round_hours_up
        assert round_hours_up(Decimal("3.3667"), Decimal("0.05")) == Decimal("3.40")


class TestMoneySourceBreakdown:
    """The owner reconciles against Square's own card / cash / service-charge
    lines, so every total must show its components and the fee withheld."""

    DAY = "2026-08-28"

    def test_day_totals_split_card_cash_and_fee(self, client, poq, staff):
        try:
            client.put("/api/settings", headers=poq["h"],
                       json={"poq_card_fee_pct": "2.2"})
            out = client.put(f"/api/days/{self.DAY}", headers=poq["h"], json={
                "credit_tips_cents": 100000, "cash_tips_cents": 5000,
                "auto_gratuity_cents": 20000,
                "shifts": [{"employee_id": staff["Ben"], "role": "SERVER", "hours": 8},
                           {"employee_id": staff["Cid"], "role": "LINE_COOK", "hours": 8}],
            }).json()["computed"]
            t = out["totals"]
            assert t["credit_tips_gross_cents"] == 100000
            assert t["card_fee_cents"] == 2200
            assert t["credit_tips_net_cents"] == 97800
            assert t["cash_tips_cents"] == 5000            # visible on its own
            assert t["auto_gratuity_gross_cents"] == 20000
            assert t["gratuity_fee_cents"] == 440
            assert t["processing_fee_total_cents"] == 2640
            # the components must reconcile to the pooled figure
            assert t["credit_tips_net_cents"] + t["cash_tips_cents"] == t["total_tips_cents"]
        finally:
            client.put("/api/settings", headers=poq["h"],
                       json={"poq_card_fee_pct": "0"})

    def test_period_totals_carry_the_same_breakdown(self, client, poq, staff):
        assert client.post(f"/api/days/{self.DAY}/finalize",
                           headers=poq["h"]).status_code == 200
        p = client.get(f"/api/periods/{self.DAY}", headers=poq["h"]).json()
        t = p["totals"]
        for key in ("credit_tips_gross_cents", "credit_tips_net_cents",
                    "cash_tips_cents", "card_fee_cents",
                    "auto_gratuity_gross_cents", "gratuity_fee_cents",
                    "processing_fee_total_cents"):
            assert key in t, key
        assert p["card_fee_pct"] is not None      # so reports can label it

    def test_csv_export_states_the_money_sources(self, client, poq):
        r = client.get(f"/api/periods/{self.DAY}/export.csv", headers=poq["h"])
        assert r.status_code == 200
        body = r.text
        for label in ("Period totals", "Card tips (gross)", "Cash tips (declared)",
                      "Auto-gratuity (gross)", "Card processing fee",
                      "Pooled tips (net)", "Kitchen (20%)"):
            assert label in body, label


class TestTipRateMetric:
    """Owner 2026-08-15: track the average tip percentage over time as a
    service-quality signal. Denominator is net sales (ex tax/tip/service
    charge), which reproduces Square's own 'Total Sales'."""

    DAY = "2026-09-04"

    def test_net_sales_is_stored_and_aggregated(self, client, poq, staff):
        r = client.put(f"/api/days/{self.DAY}", headers=poq["h"], json={
            "credit_tips_cents": 16000, "cash_tips_cents": 1000,
            "net_sales_cents": 100000,
            "shifts": [{"employee_id": staff["Ben"], "role": "SERVER", "hours": 8},
                       {"employee_id": staff["Cid"], "role": "LINE_COOK", "hours": 8}]})
        assert r.status_code == 200, r.text
        assert r.json()["computed"]["totals"]["net_sales_cents"] == 100000
        assert client.post(f"/api/days/{self.DAY}/finalize",
                           headers=poq["h"]).status_code == 200
        p = client.get(f"/api/periods/{self.DAY}", headers=poq["h"]).json()
        assert p["totals"]["net_sales_cents"] == 100000

    def test_csv_reports_the_rate(self, client, poq):
        r = client.get(f"/api/periods/{self.DAY}/export.csv", headers=poq["h"])
        body = r.text
        assert "Net sales (ex tax/tip/service charge)" in body
        # (160.00 card + 10.00 cash) / 1000.00 = 17.00%
        assert "Average tip rate (card + cash / net sales),17.00%" in body
        assert "Average tip rate incl. auto-gratuity" in body

    def test_rate_says_unavailable_rather_than_guessing(self, client, poq, staff):
        """No sales pulled -> no denominator. The report says so explicitly
        instead of printing a percentage computed from nothing. Uses the NEXT
        period so the day with sales cannot leak in."""
        client.put("/api/days/2026-09-20", headers=poq["h"], json={
            "credit_tips_cents": 5000,
            "shifts": [{"employee_id": staff["Ben"], "role": "SERVER", "hours": 5}]})
        client.post("/api/days/2026-09-20/finalize", headers=poq["h"])
        r = client.get("/api/periods/2026-09-20/export.csv", headers=poq["h"])
        assert "Average tip rate,unavailable" in r.text
        rate_line = r.text.split("Average tip rate")[1].split("\n")[0]
        assert "%" not in rate_line


class TestTipRateRefusesPartialData:
    """A day finalized before net sales were captured has tips but no sales,
    which shrinks the denominator and OVERSTATES the rate — a plausible wrong
    number is worse than none for a metric being trended (2026-08-15: a live
    period read 18.11% instead of 16.97% for exactly this reason)."""

    OLD_DAY = "2026-10-02"      # tips, no sales (pre-feature shape)
    NEW_DAY = "2026-10-03"      # tips and sales

    def test_period_lists_the_days_missing_sales(self, client, poq, staff):
        shifts = [{"employee_id": staff["Ben"], "role": "SERVER", "hours": 8}]
        client.put(f"/api/days/{self.OLD_DAY}", headers=poq["h"], json={
            "credit_tips_cents": 89047, "shifts": shifts})       # no net_sales
        client.put(f"/api/days/{self.NEW_DAY}", headers=poq["h"], json={
            "credit_tips_cents": 10000, "net_sales_cents": 60000, "shifts": shifts})
        for d in (self.OLD_DAY, self.NEW_DAY):
            assert client.post(f"/api/days/{d}/finalize",
                               headers=poq["h"]).status_code == 200
        p = client.get(f"/api/periods/{self.OLD_DAY}", headers=poq["h"]).json()
        assert p["days_missing_sales"] == [self.OLD_DAY]

    def test_csv_says_unavailable_rather_than_a_wrong_number(self, client, poq):
        r = client.get(f"/api/periods/{self.OLD_DAY}/export.csv", headers=poq["h"])
        assert "Average tip rate,unavailable" in r.text
        assert "re-pull those days" in r.text
        # the true-but-partial figure must not appear
        assert "%" not in r.text.split("Average tip rate")[1].split("\n")[0]


class TestTakeHomeColumn:
    """Owner 2026-08-15: every report should show what each person is actually
    owed, not just the component pools. Tips and gratuity stay separate — they
    are different payroll lines — but the total sits alongside them."""

    DAY = "2026-11-06"

    def test_csv_carries_take_home(self, client, poq, staff):
        client.put(f"/api/days/{self.DAY}", headers=poq["h"], json={
            "credit_tips_cents": 100000, "auto_gratuity_cents": 20000,
            "net_sales_cents": 500000,
            "shifts": [{"employee_id": staff["Ben"], "role": "SERVER", "hours": 8},
                       {"employee_id": staff["Cid"], "role": "LINE_COOK", "hours": 8}]})
        assert client.post(f"/api/days/{self.DAY}/finalize",
                           headers=poq["h"]).status_code == 200
        r = client.get(f"/api/periods/{self.DAY}/export.csv", headers=poq["h"])
        lines = r.text.strip().splitlines()
        head = lines[0].split(",")
        assert "Take Home" in head
        ti, gi, hi = (head.index("Tips Total"), head.index("Auto Gratuity (wages)"),
                      head.index("Take Home"))
        rows = [l.split(",") for l in lines[1:] if l and not l.startswith("Period totals")]
        checked = 0
        for row in rows:
            if len(row) <= hi or not row[hi]:
                continue
            try:
                tips, grat, take = float(row[ti]), float(row[gi]), float(row[hi])
            except ValueError:
                continue
            assert round(tips + grat, 2) == take, row[0]
            checked += 1
        assert checked >= 2       # the server and the cook

    def test_take_home_comes_from_one_shared_helper(self):
        """The take-home total is computed in exactly one place, so no screen
        showing it can drift from another. The printable summary is now a
        PAYROLL sheet reporting gross pay instead (owner 2026-08-16), and its
        figure comes from the server, so it cannot drift either."""
        app_js = (__import__("pathlib").Path(__file__).parent.parent
                  / "static" / "app.js").read_text()
        assert "function takeHome(s)" in app_js
        assert "fmt(takeHome(s))" in app_js
        summary = app_js.split("async function renderPrintSummary(")[1].split(
            "\nasync function ")[0]
        assert "Gross pay" in summary
        assert "s.gross_pay_cents" in summary
        assert "takeHome(" not in summary      # payroll sheet, not a pay slip


class TestCashDeclarationBreakdown:
    """Square's own labor dashboard counts a manager's HOURS but drops the
    manager's declared cash from its declared-cash tile, so its total
    disagrees with ours (2026-08-15: their $40 vs our $152) with no way to
    see why. The day screen names each declaring timecard instead."""

    def test_extractor_carries_the_declaration_onto_each_shift(self):
        from decimal import Decimal
        from app.square_extract import extract_timecards_poq
        emps = {"tm1": {"id": 1, "display_name": "Maria Munoz"},
                "tm2": {"id": 2, "display_name": "Vianeey Palalia"}}
        roles = {"Server": "SERVER", "Shift manager": "SHIFT_MANAGER"}
        tcs = [
            {"team_member_id": "tm1", "wage": {"title": "Server"},
             "start_at": "2026-08-15T16:32:00-07:00",
             "end_at": "2026-08-15T23:50:00-07:00",
             "declared_cash_tip_money": money(4000)},
            {"team_member_id": "tm2", "wage": {"title": "Shift manager"},
             "start_at": "2026-08-15T15:28:00-07:00",
             "end_at": "2026-08-16T00:09:00-07:00",
             "declared_cash_tip_money": money(11200)},
        ]
        out = extract_timecards_poq(tcs, emps, "America/Los_Angeles",
                                    Decimal("0"), roles)
        by_name = {sh["name"]: sh for sh in out["shifts"]}
        assert by_name["Maria Munoz"]["declared_cents"] == 4000
        assert by_name["Vianeey Palalia"]["declared_cents"] == 11200
        # the manager's declaration is pooled even though the job earns nothing
        assert by_name["Vianeey Palalia"]["role"] == "SHIFT_MANAGER"
        assert out["cash_tips_cents"] == 15200

    def test_a_shift_with_no_clockout_still_reports_what_it_declared(self):
        from decimal import Decimal
        from app.square_extract import extract_timecards_poq
        out = extract_timecards_poq(
            [{"team_member_id": "tm1", "wage": {"title": "Server"},
              "start_at": "2026-08-15T16:32:00-07:00",
              "declared_cash_tip_money": money(4000)}],
            {"tm1": {"id": 1, "display_name": "Maria Munoz"}},
            "America/Los_Angeles", Decimal("0"), {"Server": "SERVER"})
        assert out["shifts"][0]["declared_cents"] == 4000
        assert out["cash_tips_cents"] == 4000

    def test_day_payload_names_the_declarers_biggest_first(
            self, client, poq, staff):
        fake = client.app.state.square_client_factory()
        fake.payments, fake.orders = [], []
        fake.timecards = [
            {"team_member_id": "TM_ANA", "wage": {"title": "Server"},
             "start_at": "2026-08-27T16:32:00-07:00",
             "end_at": "2026-08-27T23:50:00-07:00",
             "declared_cash_tip_money": money(4000)},
            {"team_member_id": "TM_MGR", "wage": {"title": "Shift manager"},
             "start_at": "2026-08-27T15:28:00-07:00",
             "end_at": "2026-08-28T00:09:00-07:00",
             "declared_cash_tip_money": money(11200)},
            {"team_member_id": "TM_BEN", "wage": {"title": "Busser"},
             "start_at": "2026-08-27T17:22:00-07:00",
             "end_at": "2026-08-27T23:59:00-07:00",
             "declared_cash_tip_money": money(0)},
        ]
        assert client.post("/api/days/2026-08-27/pull",
                           headers=poq["h"]).status_code == 200
        sq = client.get("/api/days/2026-08-27", headers=poq["h"]).json()["square"]
        decs = sq["cash_declarations"]
        # only timecards that actually declared, largest first
        assert [d["cents"] for d in decs] == [11200, 4000]
        assert sum(d["cents"] for d in decs) == sq["values"]["cash_tips_cents"]
        mgr = decs[0]
        assert mgr["job_title"] == "Shift manager"
        # the shift carries the POLICY role; the screen resolves its side
        # through poq_roles, so this must be the role name, not "EXCLUDED"
        assert mgr["role"] == "SHIFT_MANAGER"
        s = client.get("/api/settings", headers=poq["h"]).json()
        assert s["poq_roles"][mgr["role"]]["side"] == "EXCLUDED"

    def test_raw_extracts_are_still_withheld_from_the_client(
            self, client, poq, staff):
        fake = client.app.state.square_client_factory()
        fake.payments, fake.orders, fake.timecards = [], [], []
        assert client.post("/api/days/2026-09-29/pull",
                           headers=poq["h"]).status_code == 200
        sq = client.get("/api/days/2026-09-29", headers=poq["h"]).json()["square"]
        assert "raw" not in sq
        assert sq["cash_declarations"] == []

    def test_day_screen_renders_the_breakdown_under_cash_tips(self):
        app_js = (__import__("pathlib").Path(__file__).parent.parent
                  / "static" / "app.js").read_text()
        poq_fn = app_js.split("async function renderDayPoq(")[1].split(
            "\nasync function ")[0]
        assert 'key === "cash_tips_cents"' in poq_fn      # placed under that field
        assert "sq.cash_declarations" in poq_fn
        assert '(roles[r.role] || {}).side === "EXCLUDED"' in poq_fn
        assert "pooled, earns nothing" in poq_fn
        css = (__import__("pathlib").Path(__file__).parent.parent
               / "static" / "styles.css").read_text()
        assert ".declarations" in css and ".decrow" in css


class TestHoursOnTheClock:
    """Two hour counts that are MEANT to differ. Tip-credited hours come from
    the pool's shifts (whole shift on its business day, earning roles only).
    Paid hours come from the clock times, split at local midnight, every job
    — that is the only figure that reconciles against the point-of-sale."""

    DAY_A, DAY_B = "2026-10-16", "2026-10-17"

    def _day(self, client, poq, staff, date, mgr_hours):
        r = client.put(f"/api/days/{date}", headers=poq["h"], json={
            "credit_tips_cents": 50000,
            "shifts": [
                {"employee_id": staff["Ana"], "role": "BARTENDER", "hours": 8},
                {"employee_id": staff["Ben"], "role": "LINE_COOK", "hours": 6},
                {"employee_id": staff["Mgr"], "role": "SHIFT_MANAGER",
                 "hours": mgr_hours},
            ]})
        assert r.status_code == 200, r.text

    def test_credited_hours_exclude_non_earning_jobs(self, client, poq, staff):
        self._day(client, poq, staff, self.DAY_A, 5)
        self._day(client, poq, staff, self.DAY_B, 4.5)
        t = client.get(f"/api/periods/{self.DAY_A}",
                       headers=poq["h"]).json()["totals"]
        assert t["credited_hours"] == 28.0      # 8 + 6, twice
        assert t["excluded_hours"] == 9.5       # the manager's two shifts

    def test_excluded_hours_come_from_shifts_not_payouts(
            self, client, poq, staff):
        """An excluded person never appears in a payout row, so reading
        hours off the payouts would silently drop them."""
        p = client.get(f"/api/periods/{self.DAY_A}", headers=poq["h"]).json()
        assert all(e["name"] != "Mgr" for e in p["employees"])
        assert p["totals"]["excluded_hours"] > 0

    def test_paid_hours_refuse_days_with_no_pull(self, client, poq, staff):
        """Hand-entered days carry no clock times, so the period cannot be
        reconciled — name the dates instead of reporting a short total."""
        t = client.get(f"/api/periods/{self.DAY_A}",
                       headers=poq["h"]).json()["totals"]
        assert self.DAY_A in t["hours_unknown_dates"]
        assert t["paid_hours"] == 0.0           # and the UI shows "—", not 0

    def test_paid_hours_split_a_shift_at_midnight(self, client, poq, staff):
        """The pool credits a 16:01→00:06 shift wholly to the night worked;
        a labor report puts those 6 minutes on the next calendar day. On the
        LAST night of a period that difference leaves the period entirely —
        which is exactly why the two totals disagree."""
        fake = client.app.state.square_client_factory()
        fake.payments, fake.orders = [], []
        fake.timecards = [
            {"team_member_id": "TM_ANA", "wage": {"title": "Bartender"},
             "start_at": "2026-11-30T16:01:00-08:00",
             "end_at": "2026-12-01T00:06:00-08:00",
             "declared_cash_tip_money": money(0)},
        ]
        assert client.post("/api/days/2026-11-30/pull",
                           headers=poq["h"]).status_code == 200
        t = client.get("/api/periods/2026-11-30",     # Nov 16-30
                       headers=poq["h"]).json()["totals"]
        # 8h05m total: 7h59m on Nov 30, 0h06m on Dec 1 — the next period
        assert t["credited_hours"] == 8.08      # pool: the whole shift
        assert t["paid_hours"] == 7.98          # labor: clipped at midnight
        assert t["hours_unknown_dates"] == []

    def test_reports_show_both_figures(self):
        app_js = (__import__("pathlib").Path(__file__).parent.parent
                  / "static" / "app.js").read_text()
        tiles = app_js.split("function poolTiles(")[1].split("\n}")[0]
        assert "Paid hours" in tiles and "Tip-credited hours" in tiles
        assert "t.overtime_hours" in tiles and "t.regular_hours" in tiles
        # a period that cannot be reconciled shows no number at all
        assert "needs re-pull" in tiles
        summary = app_js.split("async function renderPrintSummary(")[1].split(
            "\nasync function ")[0]
        assert "t.paid_hours" in summary and "t.excluded_hours" in summary
        # one shared formatter so the surfaces cannot render hours differently
        assert "function hrs(h)" in app_js

    def test_setup_exposes_the_two_overtime_settings(self, client, poq):
        for body, expect in [({"poq_workweek_start": "MON"}, 200),
                             ({"poq_workweek_start": "FUNDAY"}, 422),
                             ({"poq_overtime_after": "40"}, 200),
                             ({"poq_overtime_after": "0"}, 422),
                             ({"poq_overtime_after": "nope"}, 422)]:
            r = client.put("/api/settings", headers=poq["h"], json=body)
            assert r.status_code == expect, (body, r.text)
        client.put("/api/settings", headers=poq["h"],
                   json={"poq_workweek_start": "SUN"})


class TestPayrollSheet:
    """Owner 2026-08-16: the printable summary is what gets keyed into the
    payroll form, so it drops the pool mechanics (points, days worked) and
    carries hours, wages and gross pay instead. Wages are a cross-check —
    payroll computes what is actually paid — so they must agree with it to
    the cent or they are worse than useless."""

    def test_wages_match_a_real_pay_run(self):
        """Three people from the 2026-08-01..15 Poquitos pay run, all at
        $21.30/h with no overtime. These pin the two rounding rules: hours to
        2dp BEFORE multiplying, and the money rounded UP to the cent."""
        from datetime import date
        from engine import period_labor
        P0, P1 = date(2026, 8, 1), date(2026, 8, 15)
        # dates a week apart so nobody trips the 40 h line — these three had
        # no overtime in the real run
        cases = [
            # (name, (date, hours) pieces, Square's wages)
            ("Abel", [(7, 5.67), (8, 7.30), (15, 7.98)], 44624),
            # 53.7167 h -> 53.72 -> x 21.30 = 1144.236 -> 1144.24
            ("Alexander", [(3, 26.858333), (10, 26.858334)], 114424),
            # 53.6667 h -> 53.67 -> x 21.30 = 1143.171 -> 1143.18, NOT .17
            ("Angel", [(3, 26.833333), (10, 26.833334)], 114318),
        ]
        for name, pieces, want in cases:
            entries = [(name, date(2026, 8, day), h, 2130) for day, h in pieces]
            got = period_labor(entries, P0, P1)[name]
            assert got["overtime_hours"] == 0.0, (name, got)
            assert got["wages_cents"] == want, (name, got)

    def test_overtime_is_paid_at_time_and_a_half(self):
        from datetime import date
        from engine import period_labor
        # 45 h in one Sunday-start week at $20.00: 40 reg + 5 OT
        entries = [("ana", date(2026, 8, 3 + i), 9.0, 2000) for i in range(5)]
        got = period_labor(entries, date(2026, 8, 1), date(2026, 8, 15))["ana"]
        assert got["paid_hours"] == 45.0
        assert got["overtime_hours"] == 5.0
        assert got["regular_hours"] == 40.0
        # 40 x 20 + 5 x 30 = 950
        assert got["wages_cents"] == 95000

    def test_two_jobs_at_different_rates_are_costed_separately(self):
        from datetime import date
        from engine import period_labor
        entries = [("ana", date(2026, 8, 3), 10.0, 2130),
                   ("ana", date(2026, 8, 4), 10.0, 1850)]
        got = period_labor(entries, date(2026, 8, 1), date(2026, 8, 15))["ana"]
        assert got["wages_cents"] == 21300 + 18500

    def test_gross_pay_is_wages_plus_every_tip_line(self, client, poq, staff):
        p = client.get("/api/periods/2026-11-06/export", headers=poq["h"]).json()
        for e in p["employees"]:
            if e.get("gross_pay_cents") is None:
                continue
            assert e["gross_pay_cents"] == (
                e["wages_cents"] + e["tips_cents"]
                + e["event_cents"] + e["gratuity_cents"])

    def test_printed_sheet_drops_points_and_days(self):
        app_js = (__import__("pathlib").Path(__file__).parent.parent
                  / "static" / "app.js").read_text()
        summary = app_js.split("async function renderPrintSummary(")[1].split(
            "\nasync function ")[0]
        poq_head = summary.split("const head = isLF")[1].split("const body")[0]
        # the POINTS_HOURS arm only — the POOL_HOURS arm after it still has
        # Days and Hours columns, and should
        poq_head = poq_head.split("isPoq")[1].split('\n    : el("tr"')[0]
        # comments explain WHY those columns went; only the real labels count
        labels = "\n".join(l for l in poq_head.splitlines()
                           if not l.strip().startswith("//"))
        assert '"Points"' not in labels
        assert '"Days"' not in labels
        for col in ("Reg hrs", "OT hrs", "Wages", "Gross pay"):
            assert col in poq_head, col
        # and the sheet says what the gross is and is not
        assert "Square Payroll computes what is actually paid" in summary


class TestPayrollEntrySheet:
    """Owner 2026-08-16: a thin sheet built for typing into the payroll form —
    Reg hrs, OT hrs, Gratuity, Tips, Gross pay, in that order, with a totals
    row to check the entry against. Event money rides in the Tips column
    because the venue pays it as tips."""

    DAY = "2026-12-02"

    def _finalized_day(self, client, poq, staff):
        fake = client.app.state.square_client_factory()
        fake.payments, fake.orders = [], []
        fake.timecards = [
            {"team_member_id": "TM_ANA", "wage": {"title": "Bartender",
             "hourly_rate": {"amount": 2130, "currency": "USD"}},
             "start_at": "2026-12-02T16:00:00-08:00",
             "end_at": "2026-12-03T00:00:00-08:00",
             "declared_cash_tip_money": money(0)},
            # a manager: earns no tip share, but is still owed wages
            {"team_member_id": "TM_MGR", "wage": {"title": "Shift manager",
             "hourly_rate": {"amount": 2500, "currency": "USD"}},
             "start_at": "2026-12-02T15:00:00-08:00",
             "end_at": "2026-12-02T23:00:00-08:00",
             "declared_cash_tip_money": money(0)},
        ]
        assert client.post(f"/api/days/{self.DAY}/pull",
                           headers=poq["h"]).status_code == 200
        # keep the pulled shifts; a bare PUT would replace the whole input
        inputs = client.get(f"/api/days/{self.DAY}", headers=poq["h"]).json()["inputs"]
        inputs.update({"credit_tips_cents": 40000, "auto_gratuity_cents": 10000})
        assert client.put(f"/api/days/{self.DAY}", headers=poq["h"],
                          json=inputs).status_code == 200
        assert client.post(f"/api/days/{self.DAY}/finalize",
                           headers=poq["h"]).status_code == 200

    def test_everyone_who_worked_is_listed_even_with_no_tips(
            self, client, poq, staff):
        """A manager's shift takes no tip share but still draws wages —
        leaving them off would under-pay a real person."""
        self._finalized_day(client, poq, staff)
        p = client.get(f"/api/periods/{self.DAY}/export", headers=poq["h"]).json()
        names = {r["name"]: r for r in p["payroll"]}
        assert "Mgr" in names, "the manager must be on the payroll sheet"
        assert names["Mgr"]["tips_cents"] == 0
        assert names["Mgr"]["gratuity_cents"] == 0
        # 8 h at $25.00 and nothing else
        assert names["Mgr"]["regular_hours"] == 8.0
        assert names["Mgr"]["wages_cents"] == 20000
        assert names["Mgr"]["gross_pay_cents"] == 20000
        # but they are NOT in the tip-earner list
        assert all(e["name"] != "Mgr" for e in p["employees"])

    def test_event_money_rides_in_the_tips_column(self, client, poq, staff):
        p = client.get(f"/api/periods/{self.DAY}/export", headers=poq["h"]).json()
        earner = {e["name"]: e for e in p["employees"]}["Ana"]
        row = {r["name"]: r for r in p["payroll"]}["Ana"]
        assert row["tips_cents"] == earner["tips_cents"] + earner["event_cents"]

    def test_gross_is_wages_plus_gratuity_plus_tips(self, client, poq, staff):
        p = client.get(f"/api/periods/{self.DAY}/export", headers=poq["h"]).json()
        for r in p["payroll"]:
            assert r["gross_pay_cents"] == (
                r["wages_cents"] + r["gratuity_cents"] + r["tips_cents"]), r["name"]

    def test_the_sheet_has_the_owners_column_order_and_a_totals_row(self):
        app_js = (__import__("pathlib").Path(__file__).parent.parent
                  / "static" / "app.js").read_text()
        fn = app_js.split("async function renderPrintPayroll(")[1].split(
            "\n/* ---------- ")[0]
        head = fn.split('el("thead"')[1].split("body)")[0]
        order = [c for c in ("Employee", "Reg hrs", "OT hrs", "Gratuity",
                             "Tips", "Gross pay") if c in head]
        assert order == ["Employee", "Reg hrs", "OT hrs", "Gratuity",
                         "Tips", "Gross pay"], order
        # nothing the payroll form does not ask for
        for absent in ("Points", "Days", "Event", "Wages"):
            assert f'"{absent}"' not in head, absent
        assert 'class: "total"' in fn          # the check-figure row
        assert "rows.length} people" in fn
        m = app_js.split("const routes = {")[1].split("};")[0]
        assert '"print-payroll": renderPrintPayroll' in m
        assert "Payroll entry sheet" in app_js


class TestLaborBackfill:
    """Days finalized before clock times were stored cannot report hours.
    Re-pulling them normally would mean reopening each day and recomputing
    payouts from whatever Square says today, which can move a locked figure.
    The backfill writes ONLY the extracted shifts onto the stored pull."""

    DAY = "2027-01-05"

    def _finalized_without_clock_times(self, client, poq, staff):
        fake = client.app.state.square_client_factory()
        fake.payments, fake.orders = [], []
        fake.timecards = [
            {"team_member_id": "TM_ANA", "wage": {"title": "Bartender",
             "hourly_rate": {"amount": 2000, "currency": "USD"}},
             "start_at": "2027-01-05T17:00:00-08:00",
             "end_at": "2027-01-05T23:00:00-08:00",
             "declared_cash_tip_money": money(0)},
        ]
        assert client.post(f"/api/days/{self.DAY}/pull",
                           headers=poq["h"]).status_code == 200
        inputs = client.get(f"/api/days/{self.DAY}", headers=poq["h"]).json()["inputs"]
        inputs["credit_tips_cents"] = 30000
        client.put(f"/api/days/{self.DAY}", headers=poq["h"], json=inputs)
        assert client.post(f"/api/days/{self.DAY}/finalize",
                           headers=poq["h"]).status_code == 200
        # strip the clock times, reproducing a day finalized before they existed
        import json as _json
        from app.db import connect
        conn = connect(os.environ["DB_PATH"])
        row = conn.execute("SELECT id, square_json FROM day WHERE date = ?"
                           " AND venue_id = ?", (self.DAY, 3)).fetchone()
        rec = _json.loads(row["square_json"])
        for sh in rec["raw"]["shifts"]:
            sh.pop("start_at", None); sh.pop("end_at", None); sh.pop("rate_cents", None)
        conn.execute("UPDATE day SET square_json = ? WHERE id = ?",
                     (_json.dumps(rec), row["id"]))
        conn.commit(); conn.close()

    def test_backfill_restores_hours_without_touching_payouts(
            self, client, poq, staff):
        self._finalized_without_clock_times(client, poq, staff)
        before = client.get(f"/api/periods/{self.DAY}/export", headers=poq["h"]).json()
        assert self.DAY in before["totals"]["hours_unknown_dates"]
        assert before["totals"]["paid_hours"] == 0.0
        locked = {e["name"]: e["tips_cents"] for e in before["employees"]}
        assert locked, "the day should still have paid tips"

        r = client.post(f"/api/periods/{self.DAY}/refresh-labor", headers=poq["h"])
        assert r.status_code == 200, r.text
        assert self.DAY in r.json()["updated"]

        after = client.get(f"/api/periods/{self.DAY}/export", headers=poq["h"]).json()
        assert after["totals"]["hours_unknown_dates"] == []
        assert after["totals"]["paid_hours"] == 6.0
        # the whole point: every finalized payout is byte-identical
        assert {e["name"]: e["tips_cents"] for e in after["employees"]} == locked
        assert after["totals"]["total_tips_cents"] == before["totals"]["total_tips_cents"]

    def test_the_day_stays_finalized(self, client, poq, staff):
        d = client.get(f"/api/days/{self.DAY}", headers=poq["h"]).json()
        assert d["status"] == "finalized"

    def test_it_is_audit_logged(self, client, poq, staff):
        log = client.get("/api/audit-log?limit=50", headers=poq["h"]).json()
        entry = next(e for e in log if e["action"] == "labor_refreshed")
        assert entry["entity_id"] == self.DAY

    def test_days_never_pulled_are_skipped_not_invented(self, client, poq, staff):
        r = client.post("/api/periods/2027-02-01/refresh-labor", headers=poq["h"])
        assert r.status_code == 200
        assert r.json()["updated"] == []
        assert len(r.json()["skipped"]) > 0

    def test_other_tip_models_are_refused(self, client):
        v = {x["slug"]: x for x in client.get("/api/venues").json()}
        r = client.post("/api/periods/2027-01-05/refresh-labor",
                        headers={"X-Venue-Id": str(v["tavern-law"]["id"])})
        assert r.status_code == 422

    def test_the_export_screen_offers_it_only_when_hours_are_missing(self):
        app_js = (__import__("pathlib").Path(__file__).parent.parent
                  / "static" / "app.js").read_text()
        fn = app_js.split("async function renderExport(")[1].split(
            "\n/* ---------- ")[0]
        assert 'hours_unknown_dates || []).length' in fn
        assert "refresh-labor" in fn
        assert "stay exactly as they are" in fn      # says what it will not touch
