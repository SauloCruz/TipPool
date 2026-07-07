"""Smoke tests for the no-build frontend after the Daily Review stepper
redesign. The suite has no DOM runner, so these assert the served assets
carry the structures the design handoff requires — route registration,
fallback route, stepper markup generators, and the no-steppers rule."""

import re
from pathlib import Path

STATIC = Path(__file__).parent.parent / "static"
APP_JS = (STATIC / "app.js").read_text()
CSS = (STATIC / "styles.css").read_text()


class TestRoutes:
    def test_day_route_dispatches_per_tip_model(self):
        m = re.search(r"const routes = \{(.*?)\};", APP_JS, re.S)
        assert m, "routes table missing"
        assert re.search(r"\bday: renderDayDispatch\b", m.group(1))
        assert 'ME.venue.tip_model === "PERCENT_TIPOUT"' in APP_JS
        assert "renderDayLF" in APP_JS

    def test_venue_picker_wired(self):
        m = re.search(r"const routes = \{(.*?)\};", APP_JS, re.S)
        assert "venues: renderVenuePicker" in m.group(1)
        assert "X-Venue-Id" in APP_JS          # api() injects the scope header
        assert "venuechip" in APP_JS           # venue always visible in header
        assert "Choose a venue" in APP_JS

    def test_user_and_audit_admin_routes(self):
        m = re.search(r"const routes = \{(.*?)\};", APP_JS, re.S)
        assert "users: renderUsers" in m.group(1)
        assert "audit: renderAudit" in m.group(1)
        assert "data-super" in (STATIC / "index.html").read_text()
        assert "/api/audit-log" in APP_JS
        assert 'el("details", { class: "card usercard" }' in APP_JS
        assert ".usersummary" in CSS
        assert 'class: "audittable"' in APP_JS
        assert 'class: "auditdetail", "data-label": "Details"' in APP_JS
        assert "main.auditpage" in CSS
        assert "white-space: pre-wrap" in CSS
        assert "overflow-wrap: anywhere" in CSS

    def test_lf_screen_markers(self):
        assert "Unattributed tips" in APP_JS
        assert "unattributed_tips_unresolved" in APP_JS
        assert "no_host_resplit" in APP_JS

    def test_lf_save_preserves_hidden_pulled_inputs(self):
        lf_screen = APP_JS.split("async function renderDayLF(")[1].split(
            "async function renderDayLegacy(")[0]
        assert "hours: { ...(inputs.hours || {}) }" in lf_screen
        assert "server_tips: { ...(inputs.server_tips || {}) }" in lf_screen
        assert "delete out.hours[id]" in lf_screen

    def test_fallback_route_registered(self):
        m = re.search(r"const routes = \{(.*?)\};", APP_JS, re.S)
        assert '"day-classic": renderDayLegacy' in m.group(1)

    def test_legacy_screen_preserved_and_cross_linked(self):
        assert "async function renderDayLegacy" in APP_JS
        assert "Try new view" in APP_JS      # legacy -> stepper
        assert "Use classic view" in APP_JS  # stepper -> legacy
        # legacy's own nav must stay on the classic route
        assert "#/day-classic/${dateInput.value}" in APP_JS


class TestStepperStructure:
    def test_four_steps(self):
        assert '["Confirm", "Enter", "Review", "Lock"]' in APP_JS

    def test_footer_labels(self):
        for label in ["Confirm & continue", "Confirm $0 cash & continue",
                      "Review distribution", "Go to finalize", "Finalize — lock",
                      "Resolve clock-out to continue"]:
            assert label in APP_JS, label

    def test_clockout_resolution_affordances(self):
        assert "Record 0h — worked but never clocked out" in APP_JS
        assert "Missing clock-out — enter hours or record 0h" in APP_JS

    def test_lock_summary_items(self):
        for text in ["Clean day — straight from Square", "Zero cash tips confirmed",
                     "clock-out resolved", "Locking in"]:
            assert text in APP_JS, text

    def test_no_hour_steppers_in_new_screen(self):
        """Owner ruling: decimal keypad only — the stepper screen must not
        create ±0.25 bump buttons. (They may still exist in the legacy
        fallback, which is deleted after one pay period.)"""
        new_screen = APP_JS.split("async function renderDay(")[1].split(
            "async function renderDayLegacy(")[0]
        assert "0.25" not in new_screen
        assert 'inputmode: "decimal"' in new_screen

    def test_compliance_ui_preserved(self):
        # provenance + revert + plain-english warnings still present
        for token in ["ISSUE_TEXT", "FLAG_TEXT", "revert", "blocked_fields",
                      "src override", "severity"]:
            assert re.search(token.replace(" ", r"[\s\S]{0,40}"), APP_JS), token


class TestDesignTokens:
    def test_new_tokens_added(self):
        assert "--ok-tint" in CSS
        assert "--dot" in CSS

    def test_rail_and_step_styles_exist(self):
        for sel in [".rail", ".rail .dot", ".scard", ".hrow", ".hedit",
                    ".skipbtn", ".bohgrid", ".hero", ".locklist", ".donecircle"]:
            assert sel in CSS, sel
