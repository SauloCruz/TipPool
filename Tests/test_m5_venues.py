"""M5 integration tests: multi-venue scoping, La Fontana (PERCENT_TIPOUT)
day lifecycle with a fake Square client, unattributed-tips finalize block,
per-venue settings, and the TL export regression."""

import json
import os

import pytest
from fastapi.testclient import TestClient


def money(cents):
    return {"amount": cents, "currency": "USD"}


class FakeSquareLF:
    """Per-venue fake; records which slug the factory was asked for."""

    def __init__(self):
        self.payments = []
        self.timecards = []
        self.orders = []
        self.slugs_requested = []

    def list_payments(self, b, e):
        return self.payments

    def search_orders(self, b, e):
        return self.orders

    def search_timecards(self, b, e):
        return self.timecards

    def batch_retrieve_catalog(self, ids):
        return {"objects": [], "related_objects": []}

    def list_categories(self):
        return []

    def search_team_members(self):
        return []


@pytest.fixture(scope="module")
def fake():
    return FakeSquareLF()


@pytest.fixture(scope="module")
def client(tmp_path_factory, fake):
    data_dir = tmp_path_factory.mktemp("m5data")
    env = {
        "DATA_DIR": str(data_dir),
        "DB_PATH": str(data_dir / "m5.sqlite3"),
        "TIMEZONE": "America/Los_Angeles",
        "VENUE_NAME": "Tavern Law Test",
        "ADMIN_EMAIL": "owner@test.local",
        "ADMIN_PASSWORD": "super-secret-1",
        "SQUARE_ACCESS_TOKEN": "tl-token",
        "SQUARE_LOCATION_ID": "LOC_TL",
        "SQUARE_ACCESS_TOKEN__LA_FONTANA": "lf-token",
        "SQUARE_LOCATION_ID__LA_FONTANA": "LOC_LF",
        "NIGHTLY_SYNC": "0",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        from app.config import Settings
        from app.main import create_app

        app = create_app(Settings(env_file="/nonexistent"))

        def factory(slug):
            fake.slugs_requested.append(slug)
            return fake

        app.state.square_client_factory = factory
        with TestClient(app) as c:
            c.post("/api/login", json={"email": "owner@test.local",
                                       "password": "super-secret-1"})
            yield c
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(scope="module")
def venues(client):
    out = {v["slug"]: v for v in client.get("/api/venues").json()}
    return out


def lf_headers(venues):
    return {"X-Venue-Id": str(venues["la-fontana"]["id"])}


@pytest.fixture(scope="module")
def lf_roster(client, venues):
    ids = {}
    for name, role, tmid in [
        ("Maria", "SERVER", "TM_MARIA"), ("Paolo", "SERVER", "TM_PAOLO"),
        ("Gio", "BUSSER", "TM_GIO"), ("Lena", "BUSSER", "TM_LENA"),
        ("Rosa", "HOST", "TM_ROSA"), ("Enzo", "BOH", "TM_ENZO"),
    ]:
        r = client.post("/api/employees", headers=lf_headers(venues),
                        json={"display_name": name, "pool_role": role,
                              "square_team_member_id": tmid})
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
    return ids


DAY = "2026-07-10"


class TestVenueScoping:
    def test_both_venues_exist_with_credentials(self, client, venues):
        assert venues["tavern-law"]["tip_model"] == "POOL_HOURS"
        assert venues["la-fontana"]["tip_model"] == "PERCENT_TIPOUT"
        assert venues["la-fontana"]["square_configured"]

    def test_me_reflects_selected_venue(self, client, venues):
        me = client.get("/api/me", headers=lf_headers(venues)).json()
        assert me["venue"]["slug"] == "la-fontana"
        assert len(me["venues"]) == 2
        me_default = client.get("/api/me").json()
        assert me_default["venue"]["slug"] == "tavern-law"

    def test_settings_square_status_is_venue_scoped(self, client, venues):
        tl = client.get("/api/settings").json()["square"]
        lf = client.get("/api/settings", headers=lf_headers(venues)).json()["square"]
        assert tl["configured"] and tl["location_ids"] == ["LOC_TL"]
        assert lf["configured"] and lf["location_ids"] == ["LOC_LF"]

    def test_non_super_user_only_sees_assigned_venue(self, client, venues):
        r = client.post("/api/users", json={
            "email": "lf.only@test.local",
            "password": "manager-pass-1",
            "role": "manager",
            "venue_ids": [venues["la-fontana"]["id"]],
        })
        assert r.status_code == 201, r.text
        scoped = TestClient(client.app)
        assert scoped.post("/api/login", json={
            "email": "lf.only@test.local",
            "password": "manager-pass-1",
        }).status_code == 200
        visible = {v["slug"] for v in scoped.get("/api/venues").json()}
        assert visible == {"la-fontana"}
        assert scoped.get("/api/me").json()["venue"]["slug"] == "la-fontana"
        assert scoped.get("/api/employees", headers={
            "X-Venue-Id": str(venues["tavern-law"]["id"])
        }).status_code == 403
        assert scoped.get("/api/employees", headers=lf_headers(venues)).status_code == 200

    def test_super_admin_can_read_all_venue_audit(self, client, venues):
        r = client.get("/api/audit-log?all_venues=true")
        assert r.status_code == 200, r.text
        rows = r.json()
        assert any(row["action"] == "user_created" for row in rows)
        assert {"Tavern Law Test", "La Fontana Siciliana"} & {
            row["venue_name"] for row in rows
        }

    def test_employees_scoped_per_venue(self, client, venues, lf_roster):
        lf_names = {e["display_name"] for e in
                    client.get("/api/employees", headers=lf_headers(venues)).json()}
        tl_names = {e["display_name"] for e in client.get("/api/employees").json()}
        assert "Maria" in lf_names
        assert "Maria" not in tl_names

    def test_roles_validated_per_venue(self, client, venues):
        r = client.post("/api/employees",
                        json={"display_name": "X", "pool_role": "SERVER"})
        assert r.status_code == 422  # SERVER illegal at Tavern Law
        r = client.post("/api/employees", headers=lf_headers(venues),
                        json={"display_name": "Y", "pool_role": "FOH"})
        assert r.status_code == 422  # FOH illegal at La Fontana

    def test_unknown_venue_404(self, client):
        assert client.get("/api/employees",
                          headers={"X-Venue-Id": "999"}).status_code == 404

    def test_days_isolated_between_venues(self, client, venues, lf_roster):
        d = "2026-07-09"
        client.put(f"/api/days/{d}", headers=lf_headers(venues),
                   json={"server_tips": {lf_roster["Maria"]: 5000},
                         "hours": {lf_roster["Maria"]: 6}})
        lf_day = client.get(f"/api/days/{d}", headers=lf_headers(venues)).json()
        tl_day = client.get(f"/api/days/{d}").json()
        assert lf_day["status"] == "draft"
        assert tl_day["status"] == "not_started"


def seed_lf_square(fake):
    fake.payments = [
        # Maria: two card payments
        {"id": "P1", "status": "COMPLETED", "card_details": {},
         "team_member_id": "TM_MARIA", "tip_money": money(12000),
         "total_money": money(60000)},
        {"id": "P2", "status": "COMPLETED", "card_details": {},
         "team_member_id": "TM_MARIA", "tip_money": money(8000),
         "total_money": money(40000)},
        # Paolo: one payment
        {"id": "P3", "status": "COMPLETED", "card_details": {},
         "team_member_id": "TM_PAOLO", "tip_money": money(10000),
         "total_money": money(50000)},
        # counter sale, no team member -> unattributed
        {"id": "P4", "status": "COMPLETED", "card_details": {},
         "tip_money": money(1500), "total_money": money(9000)},
    ]
    fake.timecards = [
        {"team_member_id": "TM_MARIA", "start_at": "2026-07-10T23:00:00Z",
         "end_at": "2026-07-11T06:00:00Z", "declared_cash_tip_money": money(0),
         "wage": {"title": "Server"}},
        {"team_member_id": "TM_PAOLO", "start_at": "2026-07-10T23:00:00Z",
         "end_at": "2026-07-11T05:00:00Z", "declared_cash_tip_money": money(0),
         "wage": {"title": "Server"}},
        {"team_member_id": "TM_GIO", "start_at": "2026-07-11T00:00:00Z",
         "end_at": "2026-07-11T06:00:00Z", "declared_cash_tip_money": money(0),
         "wage": {"title": "Busser"}},
        {"team_member_id": "TM_LENA", "start_at": "2026-07-11T00:00:00Z",
         "end_at": "2026-07-11T06:00:00Z", "declared_cash_tip_money": money(0),
         # title says Host but assigned role BUSSER -> mismatch warning
         "wage": {"title": "Host"}},
        {"team_member_id": "TM_ROSA", "start_at": "2026-07-11T00:00:00Z",
         "end_at": "2026-07-11T05:30:00Z", "declared_cash_tip_money": money(0),
         "wage": {"title": "Host"}},
        {"team_member_id": "TM_ENZO", "start_at": "2026-07-10T21:00:00Z",
         "end_at": "2026-07-11T06:00:00Z", "declared_cash_tip_money": money(0),
         "wage": {"title": "Cook"}},
    ]


class TestLFPullAndFinalize:
    def test_pull_attributes_per_server(self, client, venues, lf_roster, fake):
        seed_lf_square(fake)
        fake.slugs_requested.clear()
        r = client.post(f"/api/days/{DAY}/pull", headers=lf_headers(venues))
        assert r.status_code == 200, r.text
        assert fake.slugs_requested == ["la-fontana"]  # right credentials
        body = r.json()
        assert body["inputs"]["server_tips"] == {
            str(lf_roster["Maria"]): 20000, str(lf_roster["Paolo"]): 10000}
        assert body["inputs"]["unattributed_tips_cents"] == 1500
        assert body["inputs"]["hours"][str(lf_roster["Maria"])] == 7.0
        codes = {i["code"] for i in body["square"]["issues"]}
        assert "unattributed_tips" in codes
        assert "role_mismatch" in codes      # Lena: title Host, role BUSSER
        assert "all_cash_tips_zero" in codes  # expected until policy starts

    def test_computed_pools_and_conservation(self, client, venues, lf_roster):
        body = client.get(f"/api/days/{DAY}", headers=lf_headers(venues)).json()
        t = body["computed"]["totals"]
        # 300.00 attributed: 20% busser=60.00, 10% host=30.00, 5% boh=15.00
        assert t["pool_busser_cents"] == 6000
        assert t["pool_host_cents"] == 3000
        assert t["pool_boh_cents"] == 1500
        people = {p["name"]: p for p in body["computed"]["people"]}
        assert people["Maria"]["keep_cents"] == 13000
        assert people["Gio"]["pool_share_cents"] == 3000   # even split
        assert people["Rosa"]["pool_share_cents"] == 3000
        # kitchen is paid from the monthly pool, not daily
        assert "Enzo" not in people
        total_paid = sum(p["payout_cents"] for p in body["computed"]["people"])
        assert total_paid == t["total_tips_cents"] - t["pool_boh_cents"]

    def test_finalize_blocked_until_unattributed_resolved(self, client, venues, lf_roster):
        r = client.post(f"/api/days/{DAY}/finalize", headers=lf_headers(venues))
        assert r.status_code == 422
        assert "unattributed" in r.json()["detail"]

    def test_assign_and_house_then_finalize(self, client, venues, lf_roster):
        body = client.get(f"/api/days/{DAY}", headers=lf_headers(venues)).json()
        inputs = body["inputs"]
        inputs["unattributed_assignments"] = {lf_roster["Maria"]: 1000}
        inputs["unattributed_house_cents"] = 500
        r = client.put(f"/api/days/{DAY}", headers=lf_headers(venues), json=inputs)
        assert r.status_code == 200, r.text
        computed = r.json()["computed"]
        assert not computed["flags"]["unattributed_tips_unresolved"]
        # Maria's assigned 10.00 joins her tips: keep 65% of 210.00
        people = {p["name"]: p for p in computed["people"]}
        assert people["Maria"]["keep_cents"] == 13650
        r = client.post(f"/api/days/{DAY}/finalize", headers=lf_headers(venues))
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "finalized"

    def test_lf_csv_export_components(self, client, venues, lf_roster):
        r = client.get(f"/api/periods/{DAY}/export.csv", headers=lf_headers(venues))
        assert r.status_code == 200
        assert 'filename="tips_la-fontana_' in r.headers["content-disposition"]
        lines = r.text.strip().splitlines()
        assert lines[0].split(",")[:5] == [
            "Employee", "Role", "Server Keep", "Pool Share", "Returned"]
        rows = {l.split(",")[0]: l.split(",") for l in lines[1:]}
        assert rows["Maria"][1] == "SERVER"
        assert rows["Maria"][2] == "136.50"
        # total attributed tips 310.00 (incl. Maria's assigned 10.00):
        # busser pool = 20% = 62.00, split evenly -> 31.00 each
        assert rows["Gio"][3] == "31.00"
        # weekly CSV pays daily earners; the kitchen slice (5% of 310.00 =
        # 15.50) carries to the monthly payroll report instead
        assert "Enzo" not in rows
        total = round(sum(float(r[5]) for r in rows.values()) * 100)
        assert total == 31000 - 1550

    def test_lf_percentages_setting_validated(self, client, venues):
        r = client.put("/api/settings", headers=lf_headers(venues),
                       json={"lf_percentages": {"server": "70", "busser": "20",
                                                "host": "10", "boh": "5"}})
        assert r.status_code == 422  # sums to 105
        r = client.put("/api/settings", headers=lf_headers(venues),
                       json={"lf_percentages": {"server": "70", "busser": "20",
                                                "host": "5", "boh": "5"}})
        assert r.status_code == 200


class TestMultipleSquareAccounts:
    """M5.2: one person may hold several Square team-member accounts (e.g.
    the owner appearing twice). All of them link to ONE employee; pulls sum
    hours/tips across accounts; EXCLUDED people are blocked on every account."""

    def test_second_account_links_to_existing_employee(self, client, venues, lf_roster):
        r = client.patch(f"/api/employees/{lf_roster['Maria']}",
                         headers=lf_headers(venues),
                         json={"square_team_member_id": "TM_MARIA_2ND"})
        assert r.status_code == 200, r.text
        assert set(r.json()["square_team_member_ids"]) == {"TM_MARIA", "TM_MARIA_2ND"}

    def test_duplicate_link_rejected(self, client, venues, lf_roster):
        r = client.patch(f"/api/employees/{lf_roster['Paolo']}",
                         headers=lf_headers(venues),
                         json={"square_team_member_id": "TM_MARIA_2ND"})
        assert r.status_code == 409

    def test_pull_sums_across_accounts(self, client, venues, lf_roster, fake):
        d = "2026-07-17"
        fake.payments = [
            {"id": "PA", "status": "COMPLETED", "card_details": {},
             "team_member_id": "TM_MARIA", "tip_money": money(5000),
             "total_money": money(25000)},
            {"id": "PB", "status": "COMPLETED", "card_details": {},
             "team_member_id": "TM_MARIA_2ND", "tip_money": money(3000),
             "total_money": money(15000)},
        ]
        fake.timecards = [
            {"team_member_id": "TM_MARIA", "start_at": "2026-07-17T23:00:00Z",
             "end_at": "2026-07-18T02:00:00Z", "declared_cash_tip_money": money(0)},
            {"team_member_id": "TM_MARIA_2ND", "start_at": "2026-07-18T02:00:00Z",
             "end_at": "2026-07-18T05:00:00Z", "declared_cash_tip_money": money(0)},
        ]
        fake.orders = []
        body = client.post(f"/api/days/{d}/pull", headers=lf_headers(venues)).json()
        # both accounts land on the one Maria: 50+30 tips, 3+3 hours
        assert body["inputs"]["server_tips"] == {str(lf_roster["Maria"]): 8000}
        assert body["inputs"]["hours"][str(lf_roster["Maria"])] == 6.0
        assert body["inputs"]["unattributed_tips_cents"] == 0

    def test_unlink_clears_all_accounts(self, client, venues, lf_roster):
        r = client.patch(f"/api/employees/{lf_roster['Maria']}",
                         headers=lf_headers(venues),
                         json={"square_team_member_id": ""})
        assert r.json()["square_team_member_ids"] == []
        # restore the primary link for any later tests
        client.patch(f"/api/employees/{lf_roster['Maria']}",
                     headers=lf_headers(venues),
                     json={"square_team_member_id": "TM_MARIA"})


class TestTLRegression:
    """TL behavior is frozen: same day inputs produce the same export
    content (byte-identical rows) with the venue-stamped filename."""

    def test_tl_export_content_unchanged(self, client, venues):
        for name, role in [("Bree", "FOH"), ("Kelly", "FOH"), ("Benito", "BOH")]:
            r = client.post("/api/employees",
                            json={"display_name": name, "pool_role": role})
            assert r.status_code == 201
        emps = {e["display_name"]: e["id"] for e in client.get("/api/employees").json()}
        d = "2026-07-08"
        r = client.put(f"/api/days/{d}", json={
            "food_sales_cents": 112800, "credit_tips_cents": 134563,
            "cash_tips_cents": 5900, "auto_gratuity_cents": 10800,
            "boh_worked": [emps["Benito"]],
            "foh_hours": {str(emps["Bree"]): 7.0, str(emps["Kelly"]): 7.0}})
        assert r.status_code == 200, r.text
        assert client.post(f"/api/days/{d}/finalize").status_code == 200
        r = client.get(f"/api/periods/{d}/export.csv")
        assert 'filename="tips_tavern-law_' in r.headers["content-disposition"]
        # exact rows: engine math frozen (pool 1348.23 split 7/7, boh 56.40)
        assert r.text.splitlines() == [
            "Employee,Pool Tips (FOH),Kitchen Share (BOH),Tips Total,"
            "Auto Gratuity (wages),Days Worked,FOH Hours",
            "Benito,0.00,56.40,56.40,0.00,1,0.00",
            "Bree,674.12,0.00,674.12,54.00,1,7.00",
            "Kelly,674.11,0.00,674.11,54.00,1,7.00",
        ]
