"""Contract labour: works shifts, shares the pool, paid directly against a
W-9 rather than through payroll (owner 2026-08-30).

Angelica is the live case — she worked 2026-08-29 16:00-22:00 at Tavern Law
and the app never saw her, because she has no Square account.
"""
import os

import pytest
from fastapi.testclient import TestClient


def money(c):
    return {"amount": c, "currency": "USD"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data = tmp_path_factory.mktemp("contractors")
    env = {"DATA_DIR": str(data), "DB_PATH": str(data / "c.sqlite3"),
           "TIMEZONE": "America/Los_Angeles", "VENUE_NAME": "Tavern Law Test",
           "ADMIN_EMAIL": "owner@test.local", "ADMIN_PASSWORD": "super-secret-1",
           "NIGHTLY_SYNC": "0"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        from app.config import Settings
        from app.main import create_app
        with TestClient(create_app(Settings(env_file="/nonexistent"))) as c:
            c.post("/api/login", json={"email": "owner@test.local",
                                       "password": "super-secret-1"})
            yield c
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})


@pytest.fixture(scope="module")
def tl(client):
    v = {x["slug"]: x for x in client.get("/api/venues").json()}["tavern-law"]
    return {"id": v["id"], "h": {"X-Venue-Id": str(v["id"])}}


def add(client, tl, name, **kw):
    body = {"display_name": name, "pool_role": "FOH", **kw}
    return client.post("/api/employees", headers=tl["h"], json=body)


class TestCreatingContractLabour:
    def test_a_contractor_needs_no_square_account(self, client, tl):
        r = add(client, tl, "Angelica", is_contractor=True,
                hourly_rate_cents=2000, w9_received=True)
        assert r.status_code == 201, r.text
        assert r.json()["is_contractor"] == 1

    def test_a_rate_is_required(self, client, tl):
        """Without one the $600 total silently undercounts."""
        r = add(client, tl, "No Rate", is_contractor=True)
        assert r.status_code == 422
        assert "hourly rate" in r.json()["detail"]

    def test_a_contractor_cannot_also_be_on_square(self, client, tl):
        r = add(client, tl, "Both", is_contractor=True, hourly_rate_cents=2000,
                square_team_member_id="TM_SOMEONE")
        assert r.status_code == 422
        assert "Square" in r.json()["detail"]

    def test_a_contractor_is_forced_off_the_payroll_sheet(self, client, tl):
        r = add(client, tl, "Ray", is_contractor=True, hourly_rate_cents=1800)
        assert r.json()["in_payroll"] == 0, "a 1099 worker must never reach payroll"


class TestAngelicasNight:
    """2026-08-29, 16:00-22:00 — six hours at $20, plus her share of the pool."""

    DAY = "2026-08-29"

    def _setup(self, client, tl):
        emps = {e["display_name"]: e for e in
                client.get("/api/employees", headers=tl["h"]).json()}
        if "Bree" not in emps:
            add(client, tl, "Bree")
            emps = {e["display_name"]: e for e in
                    client.get("/api/employees", headers=tl["h"]).json()}
        return emps

    def test_typed_hours_earn_a_pool_share(self, client, tl):
        emps = self._setup(client, tl)
        out = client.put(f"/api/days/{self.DAY}", headers=tl["h"], json={
            "food_sales_cents": 100000, "credit_tips_cents": 60000,
            "foh_hours": {emps["Bree"]["id"]: 6},
            "contractor_hours": {emps["Angelica"]["id"]: 6},
        }).json()["computed"]
        rows = {r["name"]: r for r in out["foh"]}
        assert rows["Angelica"]["tips_cents"] > 0
        assert rows["Angelica"]["tips_cents"] == rows["Bree"]["tips_cents"]

    def test_contractor_hours_never_override_the_pulled_map(self, client, tl):
        """The reason they get their own field: putting them in foh_hours
        would mark that whole map a manager override and freeze every other
        person's hours against the next pull."""
        emps = self._setup(client, tl)
        body = client.put(f"/api/days/{self.DAY}", headers=tl["h"], json={
            "food_sales_cents": 100000, "credit_tips_cents": 60000,
            "foh_hours": {emps["Bree"]["id"]: 6},
            "contractor_hours": {emps["Angelica"]["id"]: 6},
        }).json()
        assert str(emps["Angelica"]["id"]) not in {
            str(k) for k in body["inputs"]["foh_hours"]}
        assert body["inputs"]["contractor_hours"]


class TestThePayReport:
    DAY = "2026-08-29"

    def _finalize(self, client, tl):
        emps = {e["display_name"]: e for e in
                client.get("/api/employees", headers=tl["h"]).json()}
        client.put(f"/api/days/{self.DAY}", headers=tl["h"], json={
            "food_sales_cents": 100000, "credit_tips_cents": 60000,
            "foh_hours": {emps["Bree"]["id"]: 6},
            "contractor_hours": {emps["Angelica"]["id"]: 6},
        })
        client.post(f"/api/days/{self.DAY}/finalize", headers=tl["h"])
        return emps

    def test_wages_are_hours_times_rate(self, client, tl):
        self._finalize(client, tl)
        rows = {r["name"]: r for r in client.get(
            f"/api/periods/{self.DAY}/contractors", headers=tl["h"]
        ).json()["contractors"]}
        a = rows["Angelica"]
        assert a["hours"] == 6 and a["hourly_rate_cents"] == 2000
        assert a["wages_cents"] == 12000            # 6 h x $20

    def test_the_total_is_wages_plus_the_tip_share(self, client, tl):
        self._finalize(client, tl)
        a = {r["name"]: r for r in client.get(
            f"/api/periods/{self.DAY}/contractors", headers=tl["h"]
        ).json()["contractors"]}["Angelica"]
        assert a["tips_cents"] > 0
        assert a["total_cents"] == a["wages_cents"] + a["tips_cents"]

    def test_only_contractors_appear(self, client, tl):
        self._finalize(client, tl)
        names = {r["name"] for r in client.get(
            f"/api/periods/{self.DAY}/contractors", headers=tl["h"]
        ).json()["contractors"]}
        assert "Bree" not in names

    def test_the_600_threshold_is_reported(self, client, tl):
        self._finalize(client, tl)
        body = client.get(f"/api/periods/{self.DAY}/contractors",
                          headers=tl["h"]).json()
        assert body["threshold_cents"] == 60000
        a = {r["name"]: r for r in body["contractors"]}["Angelica"]
        assert a["ytd_total_cents"] >= a["total_cents"]
        assert a["crosses_threshold"] is (a["ytd_total_cents"] >= 60000)

    def test_w9_status_rides_along(self, client, tl):
        self._finalize(client, tl)
        a = {r["name"]: r for r in client.get(
            f"/api/periods/{self.DAY}/contractors", headers=tl["h"]
        ).json()["contractors"]}["Angelica"]
        assert a["w9_received"] is True

    def test_a_draft_night_is_not_money_owed_yet(self, client, tl):
        """Only finalized days count — an open night is not yet a payment."""
        emps = {e["display_name"]: e for e in
                client.get("/api/employees", headers=tl["h"]).json()}
        client.put("/api/days/2026-09-05", headers=tl["h"], json={
            "food_sales_cents": 50000, "credit_tips_cents": 20000,
            "contractor_hours": {emps["Angelica"]["id"]: 5},
        })
        rows = client.get("/api/periods/2026-09-05/contractors",
                          headers=tl["h"]).json()["contractors"]
        assert all(r["hours"] == 0 for r in rows if r["name"] == "Angelica")


class TestContractorsStayOffPayroll:
    """The whole point of the 1099 route: they are paid directly. A
    contractor reaching the payroll export would be paid twice — once by the
    CSV imported into payroll, once by hand."""

    DAY = "2026-08-29"

    def test_not_in_the_payroll_export(self, client, tl):
        names = {e["name"] for e in client.get(
            f"/api/periods/{self.DAY}/export", headers=tl["h"]).json()["employees"]}
        assert "Angelica" not in names
        assert "Bree" in names, "regular staff must still be there"

    def test_but_their_earnings_are_not_lost(self, client, tl):
        body = client.get(f"/api/periods/{self.DAY}/export",
                          headers=tl["h"]).json()
        assert "Angelica" in {e["name"] for e in body["contractor_employees"]}

    def test_not_in_the_payroll_csv(self, client, tl):
        csv = client.get(f"/api/periods/{self.DAY}/export.csv",
                         headers=tl["h"]).text
        assert "Angelica" not in csv


class TestKitchenContractLabour:
    """Angelica is kitchen, not front of house (owner 2026-08-30). Her share
    comes from the even kitchen split; her hours are only what she is paid."""

    DAY = "2026-09-12"

    def _setup(self, client, tl):
        emps = {e["display_name"]: e for e in
                client.get("/api/employees", headers=tl["h"]).json()}
        for name, role in (("Angie Kitchen", "BOH"), ("Benito", "BOH")):
            if name not in emps:
                body = {"display_name": name, "pool_role": role}
                if name.startswith("Angie"):
                    body |= {"is_contractor": True, "hourly_rate_cents": 2200}
                client.post("/api/employees", headers=tl["h"], json=body)
        return {e["display_name"]: e for e in
                client.get("/api/employees", headers=tl["h"]).json()}

    def _day(self, client, tl, emps, **extra):
        body = {"food_sales_cents": 100000, "credit_tips_cents": 60000,
                "foh_hours": {emps["Bree"]["id"]: 6},
                "boh_worked": [emps["Benito"]["id"], emps["Angie Kitchen"]["id"]],
                "contractor_hours": {emps["Angie Kitchen"]["id"]: 6}}
        body.update(extra)
        return client.put(f"/api/days/{self.DAY}", headers=tl["h"],
                          json=body).json()["computed"]

    def test_she_shares_the_kitchen_pool_evenly(self, client, tl):
        emps = self._setup(client, tl)
        out = self._day(client, tl, emps)
        rows = {r["name"]: r for r in out["boh"]}
        assert rows["Angie Kitchen"]["share_cents"] == rows["Benito"]["share_cents"]

    def test_her_hours_never_reach_the_foh_pool(self, client, tl):
        emps = self._setup(client, tl)
        out = self._day(client, tl, emps)
        assert "Angie Kitchen" not in {r["name"] for r in out["foh"]}
        # and the FOH pool is unchanged by her being there at all
        without = self._day(client, tl, emps, contractor_hours={})
        assert ({r["name"]: r["tips_cents"] for r in out["foh"]}
                == {r["name"]: r["tips_cents"] for r in without["foh"]})

    def test_her_hours_still_pay_her(self, client, tl):
        emps = self._setup(client, tl)
        self._day(client, tl, emps)
        client.post(f"/api/days/{self.DAY}/finalize", headers=tl["h"])
        rows = {r["name"]: r for r in client.get(
            f"/api/periods/{self.DAY}/contractors", headers=tl["h"]
        ).json()["contractors"]}
        a = rows["Angie Kitchen"]
        assert a["hours"] == 6 and a["wages_cents"] == 13200   # 6 h x $22
        assert a["tips_cents"] > 0, "her kitchen share is money she is owed"
        assert a["total_cents"] == a["wages_cents"] + a["tips_cents"]


class TestPoquitosContractLabour:
    """Poquitos reads the role off the Square job chosen at clock-in, and a
    contractor never clocks in — so the role is picked by hand and the shift
    lives in its own list (owner 2026-08-30)."""

    DAY = "2026-10-11"

    @pytest.fixture(autouse=True)
    def poq(self, client):
        v = {x["slug"]: x for x in client.get("/api/venues").json()}["poquitos"]
        self.h = {"X-Venue-Id": str(v["id"])}
        emps = {e["display_name"]: e for e in
                client.get("/api/employees", headers=self.h).json()}
        for name, role, kw in (("Reg Bartender", "FOH", {}),
                               ("Poq Cook", "BOH", {}),
                               ("Poq Contractor", "FOH",
                                {"is_contractor": True, "hourly_rate_cents": 2500})):
            if name not in emps:
                client.post("/api/employees", headers=self.h,
                            json={"display_name": name, "pool_role": role, **kw})
        self.emps = {e["display_name"]: e for e in
                     client.get("/api/employees", headers=self.h).json()}

    def _put(self, client, **extra):
        body = {"credit_tips_cents": 100000,
                "shifts": [{"employee_id": self.emps["Reg Bartender"]["id"],
                            "role": "BARTENDER", "hours": 8},
                           {"employee_id": self.emps["Poq Cook"]["id"],
                            "role": "LINE_COOK", "hours": 8}]}
        body.update(extra)
        return client.put(f"/api/days/{self.DAY}", headers=self.h, json=body).json()

    def test_a_hand_picked_role_earns_its_points(self, client):
        out = self._put(client, contractor_shifts=[
            {"employee_id": self.emps["Poq Contractor"]["id"],
             "role": "SERVER", "hours": 8}])["computed"]
        rows = {p["name"]: p for p in out["people"]}
        assert rows["Poq Contractor"]["points"] == 8       # SERVER = 1 pt/h
        assert rows["Reg Bartender"]["points"] == 10       # BARTENDER = 1.25
        assert rows["Poq Contractor"]["tips_cents"] > 0

    def test_the_pool_still_conserves(self, client):
        out = self._put(client, contractor_shifts=[
            {"employee_id": self.emps["Poq Contractor"]["id"],
             "role": "SERVER", "hours": 8}])["computed"]
        assert (sum(p["tips_cents"] for p in out["people"])
                == out["totals"]["total_tips_cents"])

    def test_the_pulled_shift_list_is_never_marked_an_override(self, client):
        """The reason contractor shifts are a separate list: a hand-added
        shift inside `shifts` would freeze the whole list against re-pulls."""
        body = self._put(client, contractor_shifts=[
            {"employee_id": self.emps["Poq Contractor"]["id"],
             "role": "SERVER", "hours": 8}])
        pulled_ids = {s["employee_id"] for s in body["inputs"]["shifts"]}
        assert self.emps["Poq Contractor"]["id"] not in pulled_ids
        assert body["inputs"]["contractor_shifts"]


class TestLaFontanaContractLabour:
    """LF pools split EVENLY among the role members who worked, so a
    contractor's hours decide membership and pay, never share size."""

    DAY = "2026-10-18"

    @pytest.fixture(autouse=True)
    def lf(self, client):
        v = {x["slug"]: x for x in client.get("/api/venues").json()}["la-fontana"]
        self.h = {"X-Venue-Id": str(v["id"])}
        have = {e["display_name"] for e in
                client.get("/api/employees", headers=self.h).json()}
        for name, role, kw in (("Sal Server", "SERVER", {}),
                               ("Bo Busser", "BUSSER", {}),
                               ("LF Contractor", "BUSSER",
                                {"is_contractor": True, "hourly_rate_cents": 2100})):
            if name not in have:
                client.post("/api/employees", headers=self.h,
                            json={"display_name": name, "pool_role": role, **kw})
        self.emps = {e["display_name"]: e for e in
                     client.get("/api/employees", headers=self.h).json()}

    def test_a_contractor_joins_their_role_pool(self, client):
        out = client.put(f"/api/days/{self.DAY}", headers=self.h, json={
            "server_tips": {self.emps["Sal Server"]["id"]: 100000},
            "hours": {self.emps["Sal Server"]["id"]: 8,
                      self.emps["Bo Busser"]["id"]: 8},
            "contractor_hours": {self.emps["LF Contractor"]["id"]: 6},
        }).json()["computed"]
        rows = {p["name"]: p for p in out["people"]}
        # even split: fewer hours does NOT mean a smaller busser share
        assert rows["LF Contractor"]["pool_share_cents"] \
            == rows["Bo Busser"]["pool_share_cents"]

    def test_hours_stay_out_of_the_pulled_map(self, client):
        body = client.put(f"/api/days/{self.DAY}", headers=self.h, json={
            "server_tips": {self.emps["Sal Server"]["id"]: 100000},
            "hours": {self.emps["Sal Server"]["id"]: 8},
            "contractor_hours": {self.emps["LF Contractor"]["id"]: 6},
        }).json()
        assert str(self.emps["LF Contractor"]["id"]) not in {
            str(k) for k in body["inputs"]["hours"]}
        assert body["inputs"]["contractor_hours"]
