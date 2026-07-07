"""LF reporting (M5.1): weekly Fri–Thu vs monthly schemes, per-venue scheme
validation, and the cash round-up on the weekly payout report."""

import os
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.periods import monthly_period_for, period_for_scheme, weekly_period_for


class TestPeriodSchemes:
    def test_weekly_friday_to_thursday(self):
        # 2026-07-10 is a Friday
        assert weekly_period_for(date(2026, 7, 10)) == (date(2026, 7, 10), date(2026, 7, 16))
        # a Thursday belongs to the week that started the PRIOR Friday
        assert weekly_period_for(date(2026, 7, 16)) == (date(2026, 7, 10), date(2026, 7, 16))
        # Saturday starts a fresh count from the day before
        assert weekly_period_for(date(2026, 7, 11)) == (date(2026, 7, 10), date(2026, 7, 16))
        assert weekly_period_for(date(2026, 7, 17)) == (date(2026, 7, 17), date(2026, 7, 23))

    def test_monthly(self):
        assert monthly_period_for(date(2026, 7, 15)) == (date(2026, 7, 1), date(2026, 7, 31))
        assert monthly_period_for(date(2028, 2, 3)) == (date(2028, 2, 1), date(2028, 2, 29))

    def test_semimonthly_untouched(self):
        assert period_for_scheme(date(2026, 7, 10), "semimonthly") == (
            date(2026, 7, 1), date(2026, 7, 15))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("lfrep")
    env = {
        "DATA_DIR": str(data_dir), "DB_PATH": str(data_dir / "t.sqlite3"),
        "TIMEZONE": "America/Los_Angeles", "VENUE_NAME": "Tavern Law Test",
        "ADMIN_EMAIL": "owner@test.local", "ADMIN_PASSWORD": "super-secret-1",
        "NIGHTLY_SYNC": "0",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        from app.config import Settings
        from app.main import create_app

        app = create_app(Settings(env_file="/nonexistent"))
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
def lf(client):
    venues = {v["slug"]: v for v in client.get("/api/venues").json()}
    H = {"X-Venue-Id": str(venues["la-fontana"]["id"])}
    ids = {}
    for name, role in [("Maria", "SERVER"), ("Gio", "BUSSER"), ("Enzo", "BOH")]:
        r = client.post("/api/employees", headers=H,
                        json={"display_name": name, "pool_role": role})
        ids[name] = r.json()["id"]
    # two finalized days: one inside the Fri-Thu week, one earlier same month
    for d, tips in [("2026-07-10", 10001), ("2026-07-06", 20000)]:
        client.put(f"/api/days/{d}", headers=H, json={
            "server_tips": {ids["Maria"]: tips},
            "hours": {ids["Maria"]: 6, ids["Gio"]: 6, ids["Enzo"]: 8}})
        assert client.post(f"/api/days/{d}/finalize",
                           headers=H).status_code == 200
    return {"H": H, "ids": ids}


class TestSchemeEndpoints:
    def test_lf_default_is_weekly(self, client, lf):
        p = client.get("/api/periods/2026-07-10", headers=lf["H"]).json()
        assert p["scheme"] == "weekly"
        assert p["schemes"] == ["weekly", "monthly"]
        assert (p["start"], p["end"]) == ("2026-07-10", "2026-07-16")

    def test_lf_monthly_covers_both_days(self, client, lf):
        p = client.get("/api/periods/2026-07-10?scheme=monthly", headers=lf["H"]).json()
        assert (p["start"], p["end"]) == ("2026-07-01", "2026-07-31")
        assert p["totals"]["total_tips_cents"] == 30001
        weekly = client.get("/api/periods/2026-07-10?scheme=weekly", headers=lf["H"]).json()
        assert weekly["totals"]["total_tips_cents"] == 10001  # Jul 6 is outside Fri-Thu week

    def test_tl_rejects_lf_schemes(self, client, lf):
        assert client.get("/api/periods/2026-07-10?scheme=weekly").status_code == 422
        p = client.get("/api/periods/2026-07-10").json()
        assert p["scheme"] == "semimonthly"

    def test_lf_rejects_semimonthly(self, client, lf):
        assert client.get("/api/periods/2026-07-10?scheme=semimonthly",
                          headers=lf["H"]).status_code == 422


class TestCashRoundUp:
    """Cash payouts are decided per employee per PERIOD on the export screen,
    pre-filled to the next amount ending in zero (507.39 -> 510). Weekly =
    front-of-house cash; monthly = kitchen cash. Payroll rows stay exact."""

    def test_suggestion_ceils_to_ten_dollars(self, client, lf):
        p = client.get("/api/periods/2026-07-10?scheme=weekly", headers=lf["H"]).json()
        rows = {e["name"]: e for e in p["employees"]}
        # Maria: 65% of 100.01 -> 65.01 -> suggested 70.00
        assert rows["Maria"]["tips_cents"] == 6501
        assert rows["Maria"]["suggested_cash_cents"] == 7000
        assert rows["Maria"]["cash_payout_cents"] == 7000
        # Gio (no host on this roster -> busser pool is 30%): 30.00 already
        # ends in zero -> suggestion stays put
        assert rows["Gio"]["tips_cents"] == 3000
        assert rows["Gio"]["cash_payout_cents"] == 3000
        for e in p["employees"]:
            assert e["cash_payout_cents"] % 1000 == 0
            assert 0 <= e["roundup_cents"] < 1000
        assert p["totals"]["total_roundup_cents"] == sum(
            e["roundup_cents"] for e in p["employees"])
        assert p["totals"]["total_cash_payout_cents"] == sum(
            e["cash_payout_cents"] for e in p["employees"])

    def test_per_period_override_persists(self, client, lf):
        # owner decides Maria gets exactly 66.00 this week
        r = client.put("/api/periods/2026-07-10/cash-payouts?scheme=weekly",
                       headers=lf["H"],
                       json={"payouts": {lf["ids"]["Maria"]: 6600}})
        assert r.status_code == 200, r.text
        rows = {e["name"]: e for e in r.json()["employees"]}
        assert rows["Maria"]["cash_payout_cents"] == 6600
        assert rows["Maria"]["roundup_cents"] == 99
        # a fresh GET reads the stored override; other rows keep suggestions
        p = client.get("/api/periods/2026-07-10?scheme=weekly", headers=lf["H"]).json()
        rows = {e["name"]: e for e in p["employees"]}
        assert rows["Maria"]["cash_payout_cents"] == 6600
        assert rows["Gio"]["cash_payout_cents"] == 3000
        # kitchen never appears on the weekly cash report
        assert "Enzo" not in rows

    def test_monthly_foh_rows_stay_exact(self, client, lf):
        p = client.get("/api/periods/2026-07-10?scheme=monthly", headers=lf["H"]).json()
        assert "total_roundup_cents" not in p["totals"]
        assert all("cash_payout_cents" not in e for e in p["employees"])

    def test_monthly_kitchen_cash_rounds(self, client, lf):
        p = client.get("/api/periods/2026-07-10/export?scheme=monthly",
                       headers=lf["H"]).json()
        enzo = next(m for m in p["boh_monthly"]["members"] if m["name"] == "Enzo")
        # share 15.00 -> suggested 20.00
        assert enzo["share_cents"] == 1500
        assert enzo["suggested_cash_cents"] == 2000
        assert enzo["cash_payout_cents"] == 2000
        assert p["boh_monthly"]["total_roundup_cents"] == 500
        # per-period override for the kitchen payout
        r = client.put("/api/periods/2026-07-10/cash-payouts?scheme=monthly",
                       headers=lf["H"],
                       json={"payouts": {lf["ids"]["Enzo"]: 1500}})
        enzo = next(m for m in r.json()["boh_monthly"]["members"]
                    if m["name"] == "Enzo")
        assert enzo["cash_payout_cents"] == 1500
        assert enzo["roundup_cents"] == 0

    def test_csv_cash_columns(self, client, lf):
        weekly = client.get("/api/periods/2026-07-10/export.csv?scheme=weekly",
                            headers=lf["H"]).text.splitlines()
        assert weekly[0].endswith("Cash Payout,Round-up")
        monthly = client.get("/api/periods/2026-07-10/export.csv?scheme=monthly",
                             headers=lf["H"]).text.strip().splitlines()
        assert monthly[0].endswith("Cash Payout,Round-up")
        foh_row = next(l for l in monthly if l.startswith("Maria"))
        assert foh_row.endswith(",,")  # payroll rows carry no cash rounding
        boh_row = next(l for l in monthly if ",BOH," in l)
        assert boh_row.split(",")[-2:] == ["15.00", "0.00"]  # Enzo override above


class TestMonthlyBohRoster:
    """Ruling 2026-07-06: BOH pool accumulates all month and is split evenly
    among a roster decided on the monthly export screen (pre-populated from
    who worked, persisted per month)."""

    def test_prepopulated_from_worked_days(self, client, lf):
        p = client.get("/api/periods/2026-07-10/export?scheme=monthly",
                       headers=lf["H"]).json()
        bm = p["boh_monthly"]
        # 5% of 100.01 -> 5.00 and 5% of 200.00 -> 10.00 = 15.00 for the month
        assert bm["allocation_cents"] == 1500
        assert bm["stored"] is False
        enzo = next(m for m in bm["members"] if m["name"] == "Enzo")
        assert enzo["selected"] and enzo["worked_days"] >= 2
        assert bm["shares"] == {str(enzo["employee_id"]): 1500}

    def test_weekly_report_has_no_boh_block(self, client, lf):
        p = client.get("/api/periods/2026-07-10/export?scheme=weekly",
                       headers=lf["H"]).json()
        assert p["boh_monthly"] is None

    def test_roster_persisted_and_editable(self, client, lf):
        # deselect everyone -> pool unassigned, flagged
        r = client.put("/api/periods/2026-07-10/boh-roster", headers=lf["H"],
                       json={"employee_ids": []})
        assert r.status_code == 200
        bm = r.json()["boh_monthly"]
        assert bm["stored"] and bm["unassigned"] and bm["shares"] == {}
        # restore Enzo
        r = client.put("/api/periods/2026-07-10/boh-roster", headers=lf["H"],
                       json={"employee_ids": [lf["ids"]["Enzo"]]})
        bm = r.json()["boh_monthly"]
        assert bm["shares"] == {str(lf["ids"]["Enzo"]): 1500}
        assert not bm["unassigned"]

    def test_monthly_csv_has_boh_rows(self, client, lf):
        lines = client.get("/api/periods/2026-07-10/export.csv?scheme=monthly",
                           headers=lf["H"]).text.strip().splitlines()
        boh_rows = [l for l in lines if ",BOH," in l]
        assert len(boh_rows) == 1
        cells = boh_rows[0].split(",")
        assert cells[0] == "Enzo"
        assert cells[5] == "15.00"  # Tips Total = monthly share

    def test_roster_validation(self, client, lf):
        r = client.put("/api/periods/2026-07-10/boh-roster", headers=lf["H"],
                       json={"employee_ids": [lf["ids"]["Maria"]]})
        assert r.status_code == 422  # Maria is a SERVER
        r = client.put("/api/periods/2026-07-10/boh-roster",
                       json={"employee_ids": []})
        assert r.status_code == 422  # TL venue has no monthly kitchen roster


class TestSalariedBohFlag:
    def test_always_in_pool_preselected_without_timecards(self, client, lf):
        # salaried chef: never clocks in, always shares the kitchen pool
        r = client.post("/api/employees", headers=lf["H"],
                        json={"display_name": "Chef Elpidio", "pool_role": "BOH"})
        chef_id = r.json()["id"]
        r = client.patch(f"/api/employees/{chef_id}", headers=lf["H"],
                         json={"always_in_boh_pool": True})
        assert r.status_code == 200
        # a month with NO stored roster: chef must be pre-selected
        p = client.get("/api/periods/2026-08-05/export?scheme=monthly",
                       headers=lf["H"]).json()
        chef = next(m for m in p["boh_monthly"]["members"]
                    if m["name"] == "Chef Elpidio")
        assert chef["selected"] and chef["always"]
        assert chef["worked_days"] == 0
        # July already has a stored roster (saved in earlier tests) — stored
        # decisions are never silently overridden by the flag
        p = client.get("/api/periods/2026-07-10/export?scheme=monthly",
                       headers=lf["H"]).json()
        chef = next(m for m in p["boh_monthly"]["members"]
                    if m["name"] == "Chef Elpidio")
        assert not chef["selected"]


class TestNoHostFlagThreshold:
    DRAFT = "2026-07-12"  # draft day: live compute reflects the setting
    # (finalized days keep the flag frozen in their immutable snapshot)

    def _make_draft(self, client, lf):
        client.put(f"/api/days/{self.DRAFT}", headers=lf["H"], json={
            "server_tips": {lf["ids"]["Maria"]: 10000},
            "hours": {lf["ids"]["Maria"]: 1, lf["ids"]["Gio"]: 1}})

    def test_resplit_is_informational_not_flagged(self, client, lf):
        # roster: 1 busser, no host; threshold 1 -> 1 busser is enough:
        # the re-split applies but the day is NOT flagged
        self._make_draft(client, lf)
        r = client.put("/api/settings", headers=lf["H"],
                       json={"lf_no_host_min_bussers": 1})
        assert r.status_code == 200, r.text
        day = client.get(f"/api/days/{self.DRAFT}", headers=lf["H"]).json()
        assert day["computed"]["flags"]["no_host_resplit"]          # reminder
        assert not day["computed"]["flags"]["no_host_low_bussers"]  # no flag
        p = client.get(f"/api/periods/{self.DRAFT}?scheme=weekly",
                       headers=lf["H"]).json()
        assert self.DRAFT not in p["flagged_dates"]
        chip = next(d for d in p["days"] if d["date"] == self.DRAFT)
        assert "no_host_resplit" not in chip["flags_on"]

    def test_low_busser_coverage_flags(self, client, lf):
        self._make_draft(client, lf)
        client.put("/api/settings", headers=lf["H"],
                   json={"lf_no_host_min_bussers": 3})
        try:
            p = client.get(f"/api/periods/{self.DRAFT}?scheme=weekly",
                           headers=lf["H"]).json()
            chip = next(d for d in p["days"] if d["date"] == self.DRAFT)
            assert "no_host_low_bussers" in chip["flags_on"]
            assert self.DRAFT in p["flagged_dates"]
        finally:
            client.put("/api/settings", headers=lf["H"],
                       json={"lf_no_host_min_bussers": 1})

    def test_no_bookkeeper_note_anywhere(self, client, lf):
        p = client.get("/api/periods/2026-07-10/export?scheme=weekly",
                       headers=lf["H"]).json()
        assert "compliance_note" not in p
        tl = client.get("/api/periods/2026-07-10/export").json()
        assert "compliance_note" not in tl


class TestNoHostRulingViaAPI:
    def test_no_host_day_effective_65_30_5(self, client, lf):
        d = "2026-07-11"
        body = client.put(f"/api/days/{d}", headers=lf["H"], json={
            "server_tips": {lf["ids"]["Maria"]: 10000},
            "hours": {lf["ids"]["Maria"]: 6, lf["ids"]["Gio"]: 6,
                      lf["ids"]["Enzo"]: 8}}).json()
        c = body["computed"]
        assert c["flags"]["no_host_resplit"]
        assert c["percentages"]["busser"] == "30"
        people = {p["name"]: p for p in c["people"]}
        assert people["Maria"]["keep_cents"] == 6500
        assert people["Gio"]["pool_share_cents"] == 3000
        # kitchen slice carries to the monthly pool
        assert "Enzo" not in people
        assert c["totals"]["pool_boh_cents"] == 500
