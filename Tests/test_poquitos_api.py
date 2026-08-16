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

    def test_screens_show_the_same_total(self):
        """Export screen and printable summary use one shared helper, so the
        number cannot drift between them."""
        app_js = (__import__("pathlib").Path(__file__).parent.parent
                  / "static" / "app.js").read_text()
        assert "function takeHome(s)" in app_js
        assert app_js.count("fmt(takeHome(s))") >= 2


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
    """The per-person Hours column counts only hours that earned a pool
    share, so it can never tie out against Square's paid-hours figure — an
    EXCLUDED job (Shift manager, Owner) works real hours and earns nothing.
    The period totals carry both numbers so the gap is visible, not a
    mystery. Reporting only: hours never re-enter the payout math."""

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

    def test_totals_split_earning_from_non_earning_hours(
            self, client, poq, staff):
        self._day(client, poq, staff, self.DAY_A, 5)
        self._day(client, poq, staff, self.DAY_B, 4.5)
        t = client.get(f"/api/periods/{self.DAY_A}",
                       headers=poq["h"]).json()["totals"]
        # 8 + 6 earning, 5 + 4.5 on the manager's excluded job
        assert t["credited_hours"] == 28.0
        assert t["excluded_hours"] == 9.5
        assert t["worked_hours"] == 37.5
        # the whole point: the two add up
        assert t["worked_hours"] == t["credited_hours"] + t["excluded_hours"]

    def test_worked_hours_come_from_the_shifts_not_the_payouts(
            self, client, poq, staff):
        """An excluded person never appears in a payout row, so reading
        hours off the payouts would silently drop them."""
        p = client.get(f"/api/periods/{self.DAY_A}", headers=poq["h"]).json()
        assert all(e["name"] != "Mgr" for e in p["employees"])
        assert p["totals"]["excluded_hours"] > 0

    def test_hours_are_absent_for_other_tip_models(self, client):
        v = {x["slug"]: x for x in client.get("/api/venues").json()}
        h = {"X-Venue-Id": str(v["tavern-law"]["id"])}
        t = client.get("/api/periods/2026-10-16", headers=h).json()["totals"]
        assert "worked_hours" not in t     # POOL_HOURS clips to a window

    def test_reports_show_both_figures(self):
        app_js = (__import__("pathlib").Path(__file__).parent.parent
                  / "static" / "app.js").read_text()
        tiles = app_js.split("function poolTiles(")[1].split("\n}")[0]
        assert "Hours on the clock" in tiles
        assert "t.worked_hours" in tiles and "t.credited_hours" in tiles
        assert "non-earning" in tiles
        summary = app_js.split("async function renderPrintSummary(")[1].split(
            "\nasync function ")[0]
        assert "Hours on the clock — all timecards" in summary
        assert "t.excluded_hours" in summary
        # one shared formatter so the surfaces cannot render hours differently
        assert "function hrs(h)" in app_js
        assert app_js.count("hrs(t.worked_hours)") >= 2
