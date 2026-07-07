"""M2 API tests: auth/roles, day lifecycle, snapshot immutability, periods,
CSV export. Runs against a temp SQLite DB per test module."""

import json
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("data")
    env = {
        "DATA_DIR": str(data_dir),
        "DB_PATH": str(data_dir / "test.sqlite3"),
        "TIMEZONE": "America/Los_Angeles",
        "VENUE_NAME": "Tavern Law Test",
        "ADMIN_EMAIL": "owner@test.local",
        "ADMIN_PASSWORD": "super-secret-1",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        from app.config import Settings
        from app.main import create_app

        app = create_app(Settings(env_file="/nonexistent"))
        with TestClient(app) as c:
            yield c
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/login", json={"email": "owner@test.local", "password": "super-secret-1"})
    assert r.status_code == 200, r.text
    return client  # cookie persists on the client


@pytest.fixture(scope="module")
def roster(admin):
    ids = {}
    for name, role in [("Bree", "FOH"), ("Kelly", "FOH"), ("Tyler", "FOH"),
                       ("Benito", "BOH"), ("Juan", "BOH"), ("Saulo", "EXCLUDED")]:
        r = admin.post("/api/employees", json={"display_name": name, "pool_role": role})
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
    r = admin.post("/api/users", json={"email": "mgr@test.local",
                                       "password": "manager-pass-1", "role": "manager"})
    assert r.status_code == 201
    return ids


DAY = "2026-06-19"


def day_inputs(ids, **over):
    base = {
        "food_sales_cents": 112800,
        "credit_tips_cents": 134563,
        "cash_tips_cents": 5900,
        "auto_gratuity_cents": 10800,
        "boh_worked": [ids["Benito"], ids["Juan"]],
        "foh_hours": {str(ids["Bree"]): 7.0, str(ids["Kelly"]): 7.0, str(ids["Tyler"]): 2.0},
    }
    base.update(over)
    return base


class TestStaticCaching:
    def test_healthz_checks_database(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["schema_version"] >= 7

    def test_static_assets_revalidate(self, client):
        """No-build SPA: stale cached CSS against new JS renders broken
        layouts. Assets must be served no-cache (revalidate every load)."""
        for path in ("/", "/static/app.js", "/static/styles.css"):
            r = client.get(path)
            assert r.status_code == 200
            assert r.headers.get("cache-control") == "no-cache", path


class TestAuth:
    def test_anonymous_rejected(self, client):
        # runs before any login in this module — no session cookie yet
        assert client.get("/api/days/2026-06-19").status_code == 401

    def test_bad_password(self, client):
        r = client.post("/api/login", json={"email": "owner@test.local", "password": "nope"})
        assert r.status_code == 401

    def test_me(self, admin):
        r = admin.get("/api/me")
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "admin"
        assert body["venue"]["name"] == "Tavern Law Test"


class TestDayLifecycle:
    def test_not_started_day(self, admin, roster):
        r = admin.get(f"/api/days/{DAY}")
        assert r.json()["status"] == "not_started"

    def test_save_draft_and_compute(self, admin, roster):
        r = admin.put(f"/api/days/{DAY}", json=day_inputs(roster))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "draft"
        t = body["computed"]["totals"]
        # engine math: tips 1404.63, boh 5% of 1128 = 56.40, pool 1348.23
        assert t["total_tips_cents"] == 140463
        assert t["boh_allocation_cents"] == 5640
        assert t["foh_pool_cents"] == 134823
        foh = {r["name"]: r for r in body["computed"]["foh"]}
        assert sum(r["tips_cents"] for r in foh.values()) == 134823
        assert foh["Tyler"]["tips_cents"] < foh["Bree"]["tips_cents"]
        boh = {r["name"]: r["share_cents"] for r in body["computed"]["boh"]}
        assert sum(boh.values()) == 5640

    def test_excluded_employee_hard_blocked(self, admin, roster):
        bad = day_inputs(roster)
        bad["foh_hours"][str(roster["Saulo"])] = 5.0
        r = admin.put(f"/api/days/{DAY}", json=bad)
        assert r.status_code == 422
        assert "Saulo" in r.json()["detail"]
        bad = day_inputs(roster, boh_worked=[roster["Benito"], roster["Saulo"]])
        assert admin.put(f"/api/days/{DAY}", json=bad).status_code == 422

    def test_wrong_pool_rejected(self, admin, roster):
        bad = day_inputs(roster, boh_worked=[roster["Bree"]])
        r = admin.put(f"/api/days/{DAY}", json=bad)
        assert r.status_code == 422
        assert "FOH, not BOH" in r.json()["detail"]

    def test_finalize_creates_snapshot_and_locks(self, admin, roster):
        r = admin.post(f"/api/days/{DAY}/finalize")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "finalized"
        assert [s["version"] for s in body["snapshots"]] == [1]
        # editing while finalized -> 409
        r = admin.put(f"/api/days/{DAY}", json=day_inputs(roster))
        assert r.status_code == 409
        # double finalize -> 409
        assert admin.post(f"/api/days/{DAY}/finalize").status_code == 409

    def test_manager_cannot_reopen_admin_can(self, admin, roster):
        mgr = TestClient(admin.app)
        r = mgr.post("/api/login", json={"email": "mgr@test.local", "password": "manager-pass-1"})
        assert r.status_code == 200
        assert mgr.post(f"/api/days/{DAY}/reopen").status_code == 403
        assert admin.post(f"/api/days/{DAY}/reopen").status_code == 200

    def test_refinalize_writes_new_version_keeps_old(self, admin, roster):
        # change cash tips and re-finalize
        edited = day_inputs(roster, cash_tips_cents=7500)
        assert admin.put(f"/api/days/{DAY}", json=edited).status_code == 200
        body = admin.post(f"/api/days/{DAY}/finalize").json()
        assert [s["version"] for s in body["snapshots"]] == [1, 2]
        # v1 must be unchanged: check via DB
        import sqlite3
        conn = sqlite3.connect(admin.app.state.settings.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT s.version, s.inputs_json FROM day_snapshot s"
            " JOIN day d ON d.id = s.day_id WHERE d.date = ? ORDER BY s.version",
            (DAY,),
        ).fetchall()
        conn.close()
        assert json.loads(rows[0]["inputs_json"])["cash_tips_cents"] == 5900
        assert json.loads(rows[1]["inputs_json"])["cash_tips_cents"] == 7500

    def test_finalized_day_serves_snapshot_not_live_compute(self, admin, roster):
        body = admin.get(f"/api/days/{DAY}").json()
        assert body["status"] == "finalized"
        assert body["computed"]["totals"]["total_tips_cents"] == 134563 + 7500


class TestRoles:
    def test_manager_cannot_manage_staff(self, admin, roster):
        mgr = TestClient(admin.app)
        mgr.post("/api/login", json={"email": "mgr@test.local", "password": "manager-pass-1"})
        assert mgr.post("/api/employees", json={"display_name": "X", "pool_role": "FOH"}).status_code == 403
        assert mgr.patch(f"/api/employees/{roster['Bree']}", json={"active": False}).status_code == 403
        assert mgr.post("/api/users", json={"email": "x@x.co", "password": "12345678", "role": "admin"}).status_code == 403

    def test_manager_can_run_a_day(self, admin, roster):
        mgr = TestClient(admin.app)
        mgr.post("/api/login", json={"email": "mgr@test.local", "password": "manager-pass-1"})
        d = "2026-06-20"
        r = mgr.put(f"/api/days/{d}", json=day_inputs(roster, credit_tips_cents=50000))
        assert r.status_code == 200
        assert mgr.post(f"/api/days/{d}/finalize").status_code == 200


class TestPeriodsAndExport:
    def test_period_summary(self, admin, roster):
        p = admin.get("/api/periods/2026-06-22").json()
        assert p["start"] == "2026-06-16" and p["end"] == "2026-06-30"
        assert len(p["days"]) == 15
        by_date = {d["date"]: d for d in p["days"]}
        assert by_date[DAY]["status"] == "finalized"
        assert by_date["2026-06-20"]["status"] == "finalized"
        assert by_date["2026-06-21"]["status"] == "not_started"
        names = {e["name"] for e in p["employees"]}
        assert {"Bree", "Kelly", "Tyler", "Benito", "Juan"} <= names
        bree = next(e for e in p["employees"] if e["name"] == "Bree")
        assert bree["days"] == 2 and bree["hours"] == 14.0

    def test_draft_days_excluded_from_export(self, admin, roster):
        # add a draft day, confirm export ignores it
        admin.put("/api/days/2026-06-21", json=day_inputs(roster, credit_tips_cents=99900))
        preview = admin.get("/api/periods/2026-06-21/export").json()
        assert "2026-06-21" in preview["draft_dates"]
        finalized_tips = preview["totals"]["total_tips_cents"]
        full = admin.get("/api/periods/2026-06-21").json()
        assert full["totals"]["total_tips_cents"] > finalized_tips

    def test_csv_export(self, admin, roster):
        r = admin.get("/api/periods/2026-06-16/export.csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        lines = r.text.strip().splitlines()
        header = lines[0].split(",")
        assert header[0] == "Employee"
        rows = {l.split(",")[0]: l.split(",") for l in lines[1:]}
        # Bree: two finalized days at 7h each
        assert float(rows["Bree"][6]) == 14.0
        # conservation across the CSV: tips column sums to pool+boh totals
        p = admin.get("/api/periods/2026-06-16/export").json()
        csv_tips = round(sum(float(r[3]) for r in rows.values()) * 100)
        assert csv_tips == p["totals"]["foh_pool_cents"] + p["totals"]["boh_allocation_cents"]

    def test_period_boundaries(self):
        from datetime import date
        from app.periods import period_for
        assert period_for(date(2026, 2, 1)) == (date(2026, 2, 1), date(2026, 2, 15))
        assert period_for(date(2026, 2, 16)) == (date(2026, 2, 16), date(2026, 2, 28))
        assert period_for(date(2028, 2, 20)) == (date(2028, 2, 16), date(2028, 2, 29))
        assert period_for(date(2026, 12, 31)) == (date(2026, 12, 16), date(2026, 12, 31))
