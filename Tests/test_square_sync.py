"""M3 integration tests: pull endpoint with a fake Square client, override
flow, blocking behavior, mapping endpoints, nightly-sync helpers, and the
v1 -> v2 DB migration."""

import json
import os
import sqlite3 as sqlite3_mod
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

DAY = "2026-07-03"  # Friday


def money(cents):
    return {"amount": cents, "currency": "USD"}


class FakeSquare:
    """Stands in for SquareClient; per-test data injected via attributes."""

    def __init__(self):
        self.payments = []
        self.orders = []
        self.timecards = []
        self.catalog_batch = {"objects": [], "related_objects": []}
        self.categories = []
        self.team = []
        self.ranges = []  # (endpoint, begin_iso, end_iso) per pull call

    def list_payments(self, b, e):
        self.ranges.append(("payments", b, e))
        return self.payments

    def search_orders(self, b, e):
        self.ranges.append(("orders", b, e))
        return self.orders

    def search_timecards(self, b, e):
        self.ranges.append(("timecards", b, e))
        return self.timecards

    def batch_retrieve_catalog(self, ids):
        return self.catalog_batch

    def list_categories(self):
        return self.categories

    # salaried staff are the only ones a timecard pull cannot see;
    # the fake mirrors the real client so the sync path is exercised
    wage_settings: dict = {}

    def retrieve_wage_setting(self, team_member_id):
        return self.wage_settings.get(team_member_id, {})

    inactive_members: list = []

    def search_team_members(self, status="ACTIVE"):
        if status != "ACTIVE":
            return self.inactive_members
        return self.team


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("m3data")
    env = {
        "DATA_DIR": str(data_dir),
        "DB_PATH": str(data_dir / "m3.sqlite3"),
        "TIMEZONE": "America/Los_Angeles",
        "VENUE_NAME": "Tavern Law Test",
        "ADMIN_EMAIL": "owner@test.local",
        "ADMIN_PASSWORD": "super-secret-1",
        "SQUARE_ACCESS_TOKEN": "fake-token",
        "SQUARE_LOCATION_ID": "LOC1",
        "NIGHTLY_SYNC": "0",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    yield env
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="module")
def fake():
    return FakeSquare()


@pytest.fixture(scope="module")
def client(env, fake):
    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(env_file="/nonexistent"))
    app.state.square_client_factory = lambda venue_slug=None: fake
    with TestClient(app) as c:
        c.post("/api/login", json={"email": "owner@test.local",
                                   "password": "super-secret-1"})
        yield c


@pytest.fixture(scope="module")
def roster(client):
    ids = {}
    for name, role, tmid in [("Bree", "FOH", "TM_BREE"), ("Kelly", "FOH", "TM_KELLY"),
                             ("Benito", "BOH", "TM_BENITO"), ("Saulo", "EXCLUDED", "TM_BOSS")]:
        r = client.post("/api/employees", json={
            "display_name": name, "pool_role": role, "square_team_member_id": tmid})
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
    return ids


def seed_square(fake):
    """A plausible Friday: burgers + beer, card tips, gratuity, three shifts."""
    fake.catalog_batch = {
        "objects": [
            {"id": "VAR_BURGER", "type": "ITEM_VARIATION",
             "item_variation_data": {"item_id": "ITEM_BURGER"}},
            {"id": "VAR_BEER", "type": "ITEM_VARIATION",
             "item_variation_data": {"item_id": "ITEM_BEER"}},
        ],
        "related_objects": [
            {"id": "ITEM_BURGER", "type": "ITEM",
             "item_data": {"name": "Burger", "reporting_category": {"id": "CAT_FOOD"}}},
            {"id": "ITEM_BEER", "type": "ITEM",
             "item_data": {"name": "IPA", "reporting_category": {"id": "CAT_BEER"}}},
            {"id": "CAT_FOOD", "type": "CATEGORY", "category_data": {"name": "Kitchen"}},
            {"id": "CAT_BEER", "type": "CATEGORY", "category_data": {"name": "Beer"}},
        ],
    }
    fake.orders = [{
        "id": "O1",
        "line_items": [
            {"catalog_object_id": "VAR_BURGER", "gross_sales_money": money(112800)},
            {"catalog_object_id": "VAR_BEER", "gross_sales_money": money(64000)},
        ],
        "service_charges": [{"name": "Auto Gratuity", "total_money": money(10800)}],
    }]
    fake.payments = [
        {"id": "P1", "status": "COMPLETED", "card_details": {},
         "tip_money": money(100000), "total_money": money(500000)},
        {"id": "P2", "status": "COMPLETED", "card_details": {},
         "tip_money": money(34563), "total_money": money(180000)},
    ]
    fake.timecards = [
        # Bree 3 PM - 12:40 AM (7.0 tippable), declared $25
        {"team_member_id": "TM_BREE", "start_at": "2026-07-03T22:00:00Z",
         "end_at": "2026-07-04T07:40:00Z", "declared_cash_tip_money": money(2500),
         "wage": {"title": "Bartender"}},
        # Kelly 5 PM - 10 PM (5.0), declared $34
        {"team_member_id": "TM_KELLY", "start_at": "2026-07-04T00:00:00Z",
         "end_at": "2026-07-04T05:00:00Z", "declared_cash_tip_money": money(3400),
         "wage": {"title": "Bartender"}},
        # Benito kitchen 11 AM - 9 PM
        {"team_member_id": "TM_BENITO", "start_at": "2026-07-03T18:00:00Z",
         "end_at": "2026-07-04T04:00:00Z", "declared_cash_tip_money": money(0),
         "wage": {"title": "Kitchen Staff"}},
    ]


class TestPullFlow:
    def test_pull_blocked_until_categories_mapped(self, client, fake, roster):
        seed_square(fake)
        r = client.post(f"/api/days/{DAY}/pull")
        assert r.status_code == 200, r.text
        body = r.json()
        sq = body["square"]
        assert "food_sales_cents" in sq["blocked_fields"]
        codes = {i["code"] for i in sq["issues"]}
        assert "unmapped_category" in codes
        # blocked field NOT applied; unblocked fields applied
        assert body["inputs"]["food_sales_cents"] == 0
        assert body["inputs"]["credit_tips_cents"] == 134563
        assert body["inputs"]["auto_gratuity_cents"] == 10800
        assert body["inputs"]["cash_tips_cents"] == 5900
        assert body["inputs"]["foh_hours"] == {str(roster["Bree"]): 7.0,
                                               str(roster["Kelly"]): 5.0}
        assert body["inputs"]["boh_worked"] == [roster["Benito"]]
        # finalize is blocked while mappings are unresolved
        r = client.post(f"/api/days/{DAY}/finalize")
        assert r.status_code == 422
        assert "mapping" in r.json()["detail"]

    def test_mapping_categories_unblocks_on_repull(self, client, fake, roster):
        r = client.put("/api/settings", json={"category_map": {
            "CAT_FOOD": {"name": "Kitchen", "group": "FOOD"},
            "CAT_BEER": {"name": "Beer", "group": "ALCOHOL"},
        }})
        assert r.status_code == 200, r.text
        body = client.post(f"/api/days/{DAY}/pull").json()
        assert body["square"]["blocked_fields"] == []
        assert body["inputs"]["food_sales_cents"] == 112800  # burgers, not beer
        t = body["computed"]["totals"]
        assert t["boh_allocation_cents"] == 5640  # 5% of 1128.00
        assert t["total_tips_cents"] == 134563 + 5900

    def test_override_survives_repull_and_is_audited(self, client, fake, roster):
        # manager corrects cash tips (e.g. a missed declaration)
        body = client.get(f"/api/days/{DAY}").json()
        inputs = body["inputs"]
        inputs["cash_tips_cents"] = 7500
        r = client.put(f"/api/days/{DAY}", json=inputs)
        assert r.status_code == 200
        # re-pull: override preserved, square value still visible
        body = client.post(f"/api/days/{DAY}/pull").json()
        assert body["inputs"]["cash_tips_cents"] == 7500
        assert body["square"]["values"]["cash_tips_cents"] == 5900
        # audit trail has the override with the square original
        db = sqlite3_mod.connect(os.environ["DB_PATH"])
        db.row_factory = sqlite3_mod.Row
        rows = db.execute("SELECT * FROM audit_log WHERE action = 'field_overridden'").fetchall()
        db.close()
        detail = json.loads(rows[-1]["detail_json"])
        assert detail["field"] == "cash_tips_cents"
        assert detail["square"] == 5900 and detail["new"] == 7500

    def test_revert_to_square_value_clears_override(self, client, fake, roster):
        body = client.get(f"/api/days/{DAY}").json()
        inputs = body["inputs"]
        inputs["cash_tips_cents"] = 5900  # back to the pulled value
        client.put(f"/api/days/{DAY}", json=inputs)
        body = client.post(f"/api/days/{DAY}/pull").json()
        assert body["inputs"]["cash_tips_cents"] == 5900

    def test_pull_is_idempotent(self, client, fake, roster):
        a = client.post(f"/api/days/{DAY}/pull").json()
        b = client.post(f"/api/days/{DAY}/pull").json()
        assert a["inputs"] == b["inputs"]
        assert a["computed"]["totals"] == b["computed"]["totals"]

    def test_finalize_after_clean_pull_then_pull_blocked(self, client, fake, roster):
        assert client.post(f"/api/days/{DAY}/finalize").status_code == 200
        assert client.post(f"/api/days/{DAY}/pull").status_code == 409

    def test_hand_typed_event_money_survives_the_first_pull(self, client, fake, roster):
        """Event money is pulled now, but a figure the manager typed before
        Square could see it is not silently zeroed on the first pull — an
        event's tips arrive as a deposit rung weeks earlier, and wiping one
        would take real money off real people."""
        d2 = "2026-07-02"
        client.put(f"/api/days/{d2}", json={"event_food_sales_cents": 50000,
                                            "event_tips_cents": 20000})
        body = client.post(f"/api/days/{d2}/pull").json()
        assert body["inputs"]["event_food_sales_cents"] == 50000
        assert body["inputs"]["event_tips_cents"] == 20000


class TestMappingEndpoints:
    def test_sync_catalog_merges_preserving_groups(self, client, fake, roster):
        fake.categories = [
            {"id": "CAT_FOOD", "category_data": {"name": "Kitchen (renamed)"}},
            {"id": "CAT_WINE", "category_data": {"name": "Wine"}},
        ]
        out = client.post("/api/square/sync-catalog").json()
        assert out["added"] == 1 and out["unmapped"] == 1
        s = client.get("/api/settings").json()
        assert s["category_map"]["CAT_FOOD"]["group"] == "FOOD"  # preserved
        assert s["category_map"]["CAT_FOOD"]["name"] == "Kitchen (renamed)"
        assert s["category_map"]["CAT_WINE"]["group"] is None

    def test_sync_team_reports_unlinked(self, client, fake, roster):
        fake.team = [
            {"id": "TM_BREE", "given_name": "Bree", "family_name": "B"},
            {"id": "TM_NEW", "given_name": "River", "family_name": "Chen"},
        ]
        out = client.post("/api/square/sync-team").json()
        assert [m["id"] for m in out["unlinked"]] == ["TM_NEW"]

    def test_settings_require_admin(self, client, roster):
        mgr = TestClient(client.app)
        client.post("/api/users", json={"email": "m3mgr@test.local",
                                        "password": "manager-pass-1", "role": "manager"})
        mgr.post("/api/login", json={"email": "m3mgr@test.local",
                                     "password": "manager-pass-1"})
        assert mgr.get("/api/settings").status_code == 200  # read ok
        assert mgr.put("/api/settings", json={"rounding_increment": "0.5"}).status_code == 403
        assert mgr.post("/api/square/sync-catalog").status_code == 403

    def test_bad_settings_rejected(self, client, roster):
        r = client.put("/api/settings", json={"category_map": {
            "X": {"name": "X", "group": "SNACKS"}}})
        assert r.status_code == 422
        r = client.put("/api/settings", json={"tippable_windows": {
            "3": {"open_minutes": 1200, "close_minutes": 1100}}})
        assert r.status_code == 422

    def test_window_change_affects_next_pull(self, client, fake, roster):
        # open Fridays at 4 PM: Bree's 3 PM - 12:40 AM shift gains an hour
        client.put("/api/settings", json={"tippable_windows": {
            "4": {"open_minutes": 16 * 60, "close_minutes": 24 * 60}}})
        d3 = "2026-07-10"  # another Friday
        old_timecards = fake.timecards
        fake.timecards = [
            {"team_member_id": "TM_BREE", "start_at": "2026-07-10T22:00:00Z",
             "end_at": "2026-07-11T07:40:00Z", "declared_cash_tip_money": money(0),
             "wage": {"title": "Bartender"}},
        ]
        try:
            body = client.post(f"/api/days/{d3}/pull").json()
            assert body["inputs"]["foh_hours"][str(roster["Bree"])] == 8.0
        finally:
            fake.timecards = old_timecards
            client.put("/api/settings", json={"tippable_windows": {
                "4": {"open_minutes": 17 * 60, "close_minutes": 24 * 60}}})


class TestDayCutoff:
    """Business-day boundary: late check settlements (12 AM - 2 AM) belong to
    the prior service day, so the pull range must extend past midnight."""

    def test_default_pull_range_is_midnight_to_midnight(self, client, fake, roster):
        fake.ranges.clear()
        client.post("/api/days/2026-07-08/pull")
        begin, end = fake.ranges[0][1], fake.ranges[0][2]
        assert begin == "2026-07-08T00:00:00-07:00"
        assert end == "2026-07-09T00:00:00-07:00"
        # all three pulls use the same window
        assert {(r[1], r[2]) for r in fake.ranges} == {(begin, end)}

    def test_2am_cutoff_shifts_pull_range(self, client, fake, roster):
        r = client.put("/api/settings", json={"day_cutoff_minutes": 120})
        assert r.status_code == 200, r.text
        try:
            fake.ranges.clear()
            client.post("/api/days/2026-07-08/pull")
            begin, end = fake.ranges[0][1], fake.ranges[0][2]
            assert begin == "2026-07-08T02:00:00-07:00"
            assert end == "2026-07-09T02:00:00-07:00"
        finally:
            client.put("/api/settings", json={"day_cutoff_minutes": 0})

    def test_cutoff_validation(self, client, roster):
        assert client.put("/api/settings",
                          json={"day_cutoff_minutes": 400}).status_code == 422
        assert client.put("/api/settings",
                          json={"day_cutoff_minutes": -30}).status_code == 422

    def test_late_settlement_lands_on_service_day(self, client, fake, roster):
        """A 1:15 AM card tip is inside the 2 AM cutoff window of the prior
        service day — the exact July-3 scenario from the dashboard."""
        client.put("/api/settings", json={"day_cutoff_minutes": 120})
        try:
            fake.payments = [
                {"id": "P_LATE", "status": "COMPLETED", "card_details": {},
                 "tip_money": money(7428), "total_money": money(40000)},
            ]
            fake.orders = []
            fake.timecards = []
            body = client.post("/api/days/2026-07-09/pull").json()
            assert body["inputs"]["credit_tips_cents"] == 7428
        finally:
            client.put("/api/settings", json={"day_cutoff_minutes": 0})
            seed_square(fake)


class TestMutedWarnings:
    DAY2 = "2026-07-07"

    def _pull_with_warning(self, client, fake):
        """Add a custom-amount (uncataloged) line item so the pull emits a
        warning-severity issue."""
        fake.orders = [dict(fake.orders[0])]
        fake.orders[0]["line_items"] = fake.orders[0]["line_items"] + [
            {"gross_sales_money": money(2000)}  # no catalog_object_id
        ]
        return client.post(f"/api/days/{self.DAY2}/pull").json()

    def test_warning_shows_by_default(self, client, fake, roster):
        seed_square(fake)
        body = self._pull_with_warning(client, fake)
        assert "uncataloged_line_items" in [i["code"] for i in body["square"]["issues"]]
        assert body["square"]["muted_count"] == 0

    def test_muted_warning_hidden_from_day_payload(self, client, fake, roster):
        r = client.put("/api/settings", json={"muted_warnings": ["uncataloged_line_items"]})
        assert r.status_code == 200, r.text
        body = client.get(f"/api/days/{self.DAY2}").json()
        assert "uncataloged_line_items" not in [i["code"] for i in body["square"]["issues"]]
        assert body["square"]["muted_count"] == 1
        # stored pull record still has everything (audit)
        db = sqlite3_mod.connect(os.environ["DB_PATH"])
        db.row_factory = sqlite3_mod.Row
        raw = json.loads(db.execute(
            "SELECT square_json FROM day WHERE date = ?", (self.DAY2,)).fetchone()[0])
        db.close()
        assert "uncataloged_line_items" in [i["code"] for i in raw["issues"]]

    def test_unmute_restores_warning(self, client, fake, roster):
        client.put("/api/settings", json={"muted_warnings": []})
        body = client.get(f"/api/days/{self.DAY2}").json()
        assert "uncataloged_line_items" in [i["code"] for i in body["square"]["issues"]]

    def test_blocking_codes_not_mutable(self, client, roster):
        for code in ("unmapped_category", "unmapped_team_member"):
            r = client.put("/api/settings", json={"muted_warnings": [code]})
            assert r.status_code == 422, code
        assert client.put("/api/settings",
                          json={"muted_warnings": ["no_such_code"]}).status_code == 422

    def test_muting_never_unblocks_finalize(self, client, fake, roster):
        """Even with every warning muted, blocking issues still gate finalize."""
        d3 = "2026-07-11"
        old_orders = fake.orders
        fake.orders = [{"id": "OX", "line_items": [
            {"catalog_object_id": "VAR_BURGER", "gross_sales_money": money(1000)}],
            "service_charges": []}]
        # unmap the food category to create a blocking issue
        s = client.get("/api/settings").json()
        try:
            client.put("/api/settings", json={
                "muted_warnings": list(s["category_groups"] and
                                       ["missing_clockout", "all_cash_tips_zero",
                                        "uncataloged_line_items"]),
                "category_map": {"CAT_FOOD": {"name": "Kitchen", "group": None},
                                 "CAT_BEER": {"name": "Beer", "group": "ALCOHOL"}}})
            body = client.post(f"/api/days/{d3}/pull").json()
            assert body["square"]["blocked_fields"] != []
            assert any(i["severity"] == "blocking" for i in body["square"]["issues"])
            assert client.post(f"/api/days/{d3}/finalize").status_code == 422
        finally:
            fake.orders = old_orders
            client.put("/api/settings", json={
                "muted_warnings": [],
                "category_map": {"CAT_FOOD": {"name": "Kitchen", "group": "FOOD"},
                                 "CAT_BEER": {"name": "Beer", "group": "ALCOHOL"}}})


class TestNightlyHelpers:
    def test_seconds_until_hour(self):
        from app.sync import seconds_until_hour
        tz = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 7, 3, 2, 0, tzinfo=tz)
        assert seconds_until_hour(now, 5) == 3 * 3600
        now = datetime(2026, 7, 3, 6, 0, tzinfo=tz)
        assert seconds_until_hour(now, 5) == 23 * 3600

    def test_nightly_targets_prior_day_and_skips_finalized(self):
        from app.sync import nightly_target_day, should_auto_sync
        tz = ZoneInfo("America/Los_Angeles")
        assert nightly_target_day(datetime(2026, 7, 4, 5, 0, tzinfo=tz)) == date(2026, 7, 3)
        assert should_auto_sync(None)
        assert should_auto_sync({"status": "draft"})
        assert not should_auto_sync({"status": "finalized"})


class TestMigration:
    def test_v1_database_upgrades_in_place(self, tmp_path):
        from app.db import SCHEMA_PATH, init_db
        db_path = tmp_path / "old.sqlite3"
        conn = sqlite3_mod.connect(db_path)
        conn.executescript(SCHEMA_PATH.read_text())
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO venue (name, timezone) VALUES ('T', 'America/Los_Angeles')")
        conn.execute(
            "INSERT INTO employee (venue_id, display_name, pool_role, created_at)"
            " VALUES (1, 'Bree', 'FOH', '2026-01-01')")
        conn.commit()
        conn.close()

        init_db(db_path, "T", "America/Los_Angeles")

        conn = sqlite3_mod.connect(db_path)
        conn.row_factory = sqlite3_mod.Row
        from app.db import SCHEMA_VERSION
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = {r[1] for r in conn.execute("PRAGMA table_info(employee)")}
        assert "square_team_member_id" in cols
        assert "round_up_cents" in cols       # v4 (legacy, unused since M5.3)
        assert "always_in_boh_pool" in cols   # v6: salaried kitchen staff
        assert "in_payroll" in cols           # v8: staff not on payroll
        # every pre-existing employee stays on the payroll sheet
        assert conn.execute(
            "SELECT in_payroll FROM employee WHERE display_name = 'Bree'"
        ).fetchone()[0] == 1
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(user)")}
        assert "super_admin" in user_cols
        # v5: existing single links copied into square_link
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "square_link" in tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(day)")}
        assert "square_json" in cols
        assert conn.execute("SELECT display_name FROM employee").fetchone()[0] == "Bree"
        # v3: venue gains slug + tip_model; other venues seeded; RBAC exists
        venues = {r["slug"]: r["tip_model"] for r in
                  conn.execute("SELECT slug, tip_model FROM venue")}
        assert venues == {"tavern-law": "POOL_HOURS",
                          "la-fontana": "PERCENT_TIPOUT",
                          "poquitos": "POINTS_HOURS"}
        assert conn.execute(
            "SELECT COUNT(*) FROM user_venue_access").fetchone()[0] == 0
        # widened role CHECK accepts LF roles, employee ids preserved
        conn.execute("INSERT INTO employee (venue_id, display_name, pool_role,"
                     " created_at) VALUES (2, 'Maria', 'SERVER', '2026-01-01')")
        conn.close()


class TestPullFailureIsActionable:
    """A pull touches whatever Square returns that day, so an unexpected shape
    must come back as something the manager can act on — not a bare 500.
    (2026-07-25: a same-minute double punch produced 'Internal Server Error'
    with no clue which employee or date was at fault.)"""

    # DAY gets finalized earlier in this module; use a fresh date
    OTHER_DAY = "2026-07-12"

    def test_zero_length_timecard_no_longer_fails_the_pull(self, client, fake, roster):
        seed_square(fake)
        fake.timecards = fake.timecards + [{
            "team_member_id": "TM_BREE",
            "start_at": "2026-07-04T08:28:00Z",
            "end_at": "2026-07-04T08:28:00Z",          # same minute — no duration
            "declared_cash_tip_money": money(0),
            "wage": {"title": "Bartender"},
        }]
        r = client.post(f"/api/days/{self.OTHER_DAY}/pull")
        assert r.status_code == 200, r.text
        codes = {i["code"] for i in r.json()["square"]["issues"]}
        assert "invalid_timecard" in codes

    def test_unexpected_error_reports_reason_and_date(self, client, fake, roster,
                                                      monkeypatch):
        from app import sync

        def boom(*a, **kw):
            raise RuntimeError("kaboom from Square payload")

        monkeypatch.setattr(sync, "pull_day", boom)
        r = client.post(f"/api/days/{self.OTHER_DAY}/pull")
        assert r.status_code == 500
        detail = r.json()["detail"]
        assert "kaboom from Square payload" in detail   # the actual reason
        assert self.OTHER_DAY in detail                 # which day to look at
        assert "not changed" in detail                  # nothing was written


class TestEventPullTavernLaw:
    """Private-event money is read out of Square rather than typed (owner
    2026-08-29). The deposit is the one part a human must place: it is rung
    days or weeks before the night, so it can never appear in the event day's
    own pull."""

    EV_DAY = "2026-07-17"
    DEP_DAY = "2026-07-10"

    def _seed_event_catalog(self, fake):
        seed_square(fake)
        fake.catalog_batch = {
            "objects": [
                {"id": "VAR_BURGER", "type": "ITEM_VARIATION",
                 "item_variation_data": {"item_id": "ITEM_BURGER"}},
                {"id": "VAR_EVFOOD", "type": "ITEM_VARIATION",
                 "item_variation_data": {"item_id": "ITEM_EVFOOD"}},
                {"id": "VAR_EVBEV", "type": "ITEM_VARIATION",
                 "item_variation_data": {"item_id": "ITEM_EVBEV"}},
                {"id": "VAR_EVDEP", "type": "ITEM_VARIATION",
                 "item_variation_data": {"item_id": "ITEM_EVDEP"}},
            ],
            "related_objects": [
                {"id": "ITEM_BURGER", "type": "ITEM",
                 "item_data": {"name": "Burger",
                               "reporting_category": {"id": "CAT_FOOD"}}},
                {"id": "ITEM_EVFOOD", "type": "ITEM",
                 "item_data": {"name": "Event Food Packages",
                               "reporting_category": {"id": "CAT_EVENT"}}},
                {"id": "ITEM_EVBEV", "type": "ITEM",
                 "item_data": {"name": "Event Beverage Package",
                               "reporting_category": {"id": "CAT_EVENT"}}},
                {"id": "ITEM_EVDEP", "type": "ITEM",
                 "item_data": {"name": "Event Deposit",
                               "reporting_category": {"id": "CAT_EVENT"}}},
                {"id": "CAT_FOOD", "type": "CATEGORY",
                 "category_data": {"name": "Kitchen"}},
                {"id": "CAT_EVENT", "type": "CATEGORY",
                 "category_data": {"name": "Events & Catering"}},
            ],
        }

    def _map(self, client):
        client.put("/api/settings", json={"category_map": {
            "CAT_FOOD": {"name": "Kitchen", "group": "FOOD"},
            "CAT_EVENT": {"name": "Events & Catering", "group": "EVENT"},
            "CAT_BEER": {"name": "Beer", "group": "ALCOHOL"}}})

    def _pull_deposit_day(self, client, fake):
        fake.orders = [{
            "id": "ODEP", "created_at": "2026-07-11T04:27:36Z",
            "tenders": [{"id": "3p1baGg6Avl", "note": "O'Brien Deposit for 7/17"}],
            "line_items": [{"uid": "d1", "catalog_object_id": "VAR_EVDEP",
                            "gross_sales_money": money(56924),
                            "note": "O'Brien Deposit for 7/17"}],
        }]
        fake.payments, fake.timecards = [], []
        return client.post(f"/api/days/{self.DEP_DAY}/pull")

    def _pull_event_day(self, client, fake):
        fake.orders = [
            # the party's own tickets: food package, beverage package, and a
            # gratuity that belongs to the crew who worked the party
            {"id": "OEV", "created_at": "2026-07-18T02:00:00Z",
             "line_items": [
                 {"uid": "e1", "catalog_object_id": "VAR_EVFOOD",
                  "gross_sales_money": money(44000)},
                 {"uid": "e2", "catalog_object_id": "VAR_EVBEV",
                  "gross_sales_money": money(157500)}]},
            {"id": "OEV2", "created_at": "2026-07-18T02:10:00Z",
             "line_items": [{"uid": "e3", "catalog_object_id": "VAR_EVFOOD",
                             "gross_sales_money": money(7500)}],
             "service_charges": [{"type": "AUTO_GRATUITY",
                                  "applied_money": money(5580)}]},
            # ordinary trade the same night
            {"id": "OBAR", "created_at": "2026-07-18T03:00:00Z",
             "line_items": [{"uid": "b1", "catalog_object_id": "VAR_BURGER",
                             "gross_sales_money": money(9000)}],
             "service_charges": [{"type": "AUTO_GRATUITY",
                                  "applied_money": money(2660)}]},
        ]
        fake.payments = [
            {"id": "PB", "order_id": "OBAR", "status": "COMPLETED",
             "card_details": {}, "tip_money": money(4000),
             "total_money": money(15660)},
            {"id": "PE", "order_id": "OEV2", "status": "COMPLETED",
             "card_details": {}, "tip_money": money(0),
             "total_money": money(13080)},
        ]
        fake.timecards = []
        return client.post(f"/api/days/{self.EV_DAY}/pull")

    def test_event_lines_split_out_of_food_and_tips(self, client, fake, roster):
        self._seed_event_catalog(fake)
        self._map(client)
        body = self._pull_event_day(client, fake).json()
        inp = body["inputs"]
        assert inp["food_sales_cents"] == 9000            # the burger only
        assert inp["event_food_sales_cents"] == 51500     # 440 + 75, both lines
        assert inp["event_tips_cents"] == 5580            # the event's gratuity
        assert inp["auto_gratuity_cents"] == 2660         # the bar's, not the party's
        assert inp["credit_tips_cents"] == 4000           # event ticket excluded
        ev = body["square"]["event"]
        assert ev["other_cents"] == 157500                # beverage package
        assert ev["other_lines"][0]["item"] == "Event Beverage Package"

    def test_deposit_is_offered_then_attached(self, client, fake, roster):
        self._seed_event_catalog(fake)
        self._map(client)
        self._pull_deposit_day(client, fake)
        self._pull_event_day(client, fake)

        listing = client.get(f"/api/days/{self.EV_DAY}/event-deposits").json()
        dep, = listing["deposits"]
        assert dep["deposit_id"] == "ODEP:d1"
        assert dep["gross_cents"] == 56924
        assert dep["rung_on"] == self.DEP_DAY
        assert dep["attached_to"] is None
        assert dep["suggested"] is True            # its note names 7/17

        r = client.put(f"/api/days/{self.EV_DAY}/event-deposits",
                       json={"deposit_ids": ["ODEP:d1"]})
        assert r.status_code == 200, r.text
        # ticket gratuity + the deposit, and nothing typed by hand
        assert r.json()["inputs"]["event_tips_cents"] == 5580 + 56924
        assert r.json()["inputs"]["event_deposit_ids"] == ["ODEP:d1"]

    def test_a_deposit_cannot_be_paid_to_two_events(self, client, fake, roster):
        self._seed_event_catalog(fake)
        self._map(client)
        self._pull_deposit_day(client, fake)
        self._pull_event_day(client, fake)
        client.put(f"/api/days/{self.EV_DAY}/event-deposits",
                   json={"deposit_ids": ["ODEP:d1"]})
        other = "2026-07-19"
        self._pull_event_day(client, fake)   # same fixture, different day
        client.post(f"/api/days/{other}/pull")
        r = client.put(f"/api/days/{other}/event-deposits",
                       json={"deposit_ids": ["ODEP:d1"]})
        assert r.status_code == 409
        assert self.EV_DAY in r.json()["detail"]

    def test_detaching_gives_the_money_back(self, client, fake, roster):
        self._seed_event_catalog(fake)
        self._map(client)
        self._pull_deposit_day(client, fake)
        self._pull_event_day(client, fake)
        client.put(f"/api/days/{self.EV_DAY}/event-deposits",
                   json={"deposit_ids": ["ODEP:d1"]})
        r = client.put(f"/api/days/{self.EV_DAY}/event-deposits",
                       json={"deposit_ids": []})
        assert r.json()["inputs"]["event_tips_cents"] == 5580
        listing = client.get(f"/api/days/{self.EV_DAY}/event-deposits").json()
        assert listing["deposits"][0]["attached_to"] is None

    def test_repull_keeps_the_attached_deposit(self, client, fake, roster):
        self._seed_event_catalog(fake)
        self._map(client)
        self._pull_deposit_day(client, fake)
        self._pull_event_day(client, fake)
        client.put(f"/api/days/{self.EV_DAY}/event-deposits",
                   json={"deposit_ids": ["ODEP:d1"]})
        body = self._pull_event_day(client, fake).json()
        assert body["inputs"]["event_tips_cents"] == 5580 + 56924


class TestTimecardsCarryClockTimesEverywhere:
    """Paid hours, overtime and wages need clock times and the job's rate.
    The points model stored them first; the other two now do too, so every
    venue can run the payroll sheet and the timecard backfill."""

    def test_tavern_law_timecards_carry_times_and_rate(self):
        from app.square_extract import extract_timecards
        from engine import TippableWindow
        from datetime import date
        from decimal import Decimal
        emp = {"TM1": {"id": 7, "display_name": "Ana", "pool_role": "FOH"}}
        tcs = [{"team_member_id": "TM1", "start_at": "2026-08-20T17:00:00-07:00",
                "end_at": "2026-08-20T23:30:00-07:00",
                "wage": {"title": "Bartender",
                         "hourly_rate": {"amount": 2130, "currency": "USD"}},
                "declared_cash_tip_money": {"amount": 0, "currency": "USD"}}]
        out = extract_timecards(
            tcs, emp, date(2026, 8, 20),
            {i: TippableWindow() for i in range(7)},
            "America/Los_Angeles", Decimal("0.05"),
            job_roles={"Bartender": "FOH"})
        card = out["timecards"][0]
        assert card["start_at"] == "2026-08-20T17:00:00-07:00"
        assert card["end_at"] == "2026-08-20T23:30:00-07:00"
        assert card["rate_cents"] == 2130

    def test_la_fontana_timecards_carry_times_and_rate(self):
        from app.square_extract import extract_lf_timecards
        emp = {"TM1": {"id": 9, "display_name": "Bo", "pool_role": "SERVER"}}
        tcs = [{"team_member_id": "TM1", "start_at": "2026-08-20T16:00:00-07:00",
                "end_at": "2026-08-20T22:00:00-07:00",
                "wage": {"title": "Server",
                         "hourly_rate": {"amount": 1800, "currency": "USD"}},
                "declared_cash_tip_money": {"amount": 0, "currency": "USD"}}]
        card = extract_lf_timecards(tcs, emp)["timecards"][0]
        assert card["start_at"] and card["end_at"]
        assert card["rate_cents"] == 1800

    def test_a_missing_rate_is_none_not_a_guess(self):
        from app.square_extract import _rate_cents
        assert _rate_cents({"wage": {"title": "Host"}}) is None
        assert _rate_cents({}) is None
