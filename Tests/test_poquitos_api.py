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
