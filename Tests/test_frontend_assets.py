"""Smoke tests for the no-build frontend after the Daily Review stepper
redesign. The suite has no DOM runner, so these assert the served assets
carry the structures the design handoff requires — route registration,
stepper markup generators, and the no-steppers rule."""

import json
import re
from pathlib import Path

STATIC = Path(__file__).parent.parent / "static"
APP_JS = (STATIC / "app.js").read_text()
CSS = (STATIC / "styles.css").read_text()
INDEX = (STATIC / "index.html").read_text()


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
            "/* ---------- period dashboard ---------- */")[0]
        assert "hours: { ...(inputs.hours || {}) }" in lf_screen
        assert "server_tips: { ...(inputs.server_tips || {}) }" in lf_screen
        assert "delete out.hours[id]" in lf_screen

    def test_legacy_daily_review_retired(self):
        # Retired 2026-07-07 (owner) — the stepper is the only day screen.
        assert "day-classic" not in APP_JS
        assert "renderDayLegacy" not in APP_JS
        assert "classic view" not in APP_JS


class TestConsolidatedDayScreen:
    """Tavern Law's day screen is ONE page, not a four-step wizard (owner
    2026-08-30). Square pulls six of the seven daily figures now, so the
    walk-through was mostly clicking past values that were already correct.

    The wizard's per-step gates were real safeguards, so they must survive the
    consolidation as blocks on the only irreversible action. These assert they
    are still there — losing one silently is how a $0 cash night or a missing
    clock-out gets locked in unnoticed.
    """

    SCREEN = APP_JS.split("async function renderDay(")[1].split(
        "async function renderDayLF(")[0]

    def test_no_step_wizard_left(self):
        """Scoped to the Tavern Law screen on purpose: La Fontana still runs
        its own four-step stepper and keeps its labels."""
        assert "const STEP_LABELS" not in APP_JS
        for gone in ("Confirm & continue", "Review distribution ›",
                     "Go to finalize", "class: \"rail\"", "goTo("):
            assert gone not in self.SCREEN, gone

    def test_every_section_is_on_the_page(self):
        for label in ["Tonight's money — from Square", "Hours & manual entries",
                      "Distribution", "What gets locked"]:
            assert label in self.SCREEN, label

    def test_missing_clockout_blocks_finalize(self):
        assert "to finalize" in self.SCREEN
        assert "unresolvedClockouts()" in self.SCREEN
        assert "Record 0h — worked but never clocked out" in APP_JS
        assert "Missing clock-out — enter hours or record 0h" in APP_JS

    def test_zero_cash_still_needs_an_explicit_yes(self):
        """The gate became two presses of one button rather than two screens,
        but the manager still has to say so."""
        assert "Confirm $0 cash tips, then finalize" in self.SCREEN
        assert "cashGateOpen()" in self.SCREEN

    def test_unmapped_mappings_still_block(self):
        assert "Blocked — fix mappings in Setup" in self.SCREEN

    def test_lock_summary_items(self):
        for text in ["Clean day — straight from Square", "Zero cash tips confirmed",
                     "clock-out resolved", "What gets locked"]:
            assert text in APP_JS, text

    def test_event_block_collapses_but_opens_itself_when_it_matters(self):
        """A $0 section is scroll between the manager and the button — but an
        unattached deposit is money nobody pays out, so it must not hide."""
        assert "nothing tonight" in self.SCREEN
        assert "deposits" in self.SCREEN

    def test_no_hour_steppers(self):
        """Owner ruling: decimal keypad only — no ±0.25 bump buttons."""
        assert "0.25" not in self.SCREEN
        assert 'inputmode: "decimal"' in self.SCREEN

    def test_compliance_ui_preserved(self):
        for token in ["ISSUE_TEXT", "FLAG_TEXT", "revert", "blocked_fields",
                      "src override", "severity"]:
            assert re.search(token.replace(" ", r"[\s\S]{0,40}"), APP_JS), token


class TestPrintViews:
    def test_print_routes_registered(self):
        m = re.search(r"const routes = \{(.*?)\};", APP_JS, re.S)
        assert '"print-summary": renderPrintSummary' in m.group(1)
        assert '"print-4070": renderPrint4070' in m.group(1)

    def test_form_4070_structure(self):
        for text in ["Employee's Report of Tips to Employer",
                     "Facsimile of IRS Form 4070",
                     "Social security number", "Tips paid out to other employees",
                     "Net tips (lines 1 + 2 − 3)",
                     "verify filing requirements"]:
            assert text in APP_JS, text
        # SSN/address are blank lines, never data-bound
        assert "f.ssn" not in APP_JS and "f.address" not in APP_JS

    def test_summary_sheet_structure(self):
        for text in ["Tip Distribution Summary", "Reviewed and approved",
                     "Print / Save as PDF"]:
            assert text in APP_JS, text

    def test_print_css(self):
        for sel in [".sheet", ".printbar", "page-break-after: always",
                    "@media print"]:
            assert sel in CSS, sel


class TestAddToHomeScreen:
    """iOS 'Add to Home Screen' as a standalone web app: Apple meta tags,
    a touch icon, and a linked PWA manifest with the expected icon set."""

    def test_index_head_tags(self):
        for token in ['name="apple-mobile-web-app-capable" content="yes"',
                      'name="apple-mobile-web-app-title" content="TipPool"',
                      'name="apple-mobile-web-app-status-bar-style"',
                      'rel="apple-touch-icon" href="/static/icon-180.png"',
                      'rel="manifest" href="/static/manifest.webmanifest"']:
            assert token in INDEX, token

    def test_manifest_and_icons_present(self):
        man = json.loads((STATIC / "manifest.webmanifest").read_text())
        assert man["display"] == "standalone"
        srcs = {i["src"] for i in man["icons"]}
        assert {"/static/icon-192.png", "/static/icon-512.png"} <= srcs
        assert any(i.get("purpose") == "maskable" for i in man["icons"])
        for name in ("icon-180.png", "icon-192.png", "icon-512.png"):
            assert (STATIC / name).exists(), name


class TestDesignTokens:
    def test_new_tokens_added(self):
        assert "--ok-tint" in CSS
        assert "--dot" in CSS

    def test_rail_and_step_styles_exist(self):
        for sel in [".rail", ".rail .dot", ".scard", ".hrow", ".hedit",
                    ".skipbtn", ".bohgrid", ".hero", ".locklist", ".donecircle"]:
            assert sel in CSS, sel


class TestPoquitosDayScreen:
    """POINTS_HOURS day screen (Poquitos, M6). It must offer the same day
    controls as the other two venues — an admin has to be able to reopen a
    locked day, and dates must be navigable without a trip via Period."""

    SCREEN = APP_JS.split("async function renderDayPoq(")[1].split(
        "/* ---------- period dashboard ---------- */")[0]

    def test_registered_for_the_points_model(self):
        assert '"POINTS_HOURS") return renderDayPoq' in APP_JS

    def test_admin_can_reopen_a_locked_day(self):
        assert "/reopen" in self.SCREEN
        assert "Reopen day" in self.SCREEN
        assert 'ME.role !== "admin"' in self.SCREEN   # managers cannot
        assert "View period" in self.SCREEN

    def test_date_navigation_present(self):
        assert "shift(-1)" in self.SCREEN and "shift(1)" in self.SCREEN
        assert "showPicker" in self.SCREEN

    def test_card_fee_is_spelled_out_not_silent(self):
        """Both fee lines must be visible — a deduction that moves real money
        should never appear only as a smaller pool."""
        assert "Processing fee" in self.SCREEN
        assert "card tips" in self.SCREEN and "gratuity" in self.SCREEN
        assert "Cash tips are exempt" in self.SCREEN

    def test_role_and_points_shown_per_shift(self):
        assert "rolechip" in self.SCREEN
        assert "pt/h" in self.SCREEN

    def test_blocking_issue_stops_finalize(self):
        assert "Blocked — fix mappings in Setup" in self.SCREEN


class TestPeriodScreenPerModel:
    """Each tip model names its pools differently. The period screen must
    branch on the model — POINTS_HOURS has no `boh_allocation_cents`, so
    falling through to the hourly-pool labels showed its kitchen share as
    $0.00 and its per-employee tips as NaN (2026-08-14 regression)."""

    TILES = APP_JS.split("function poolTiles(")[1].split("\n}")[0]

    def test_points_hours_reads_its_own_kitchen_key(self):
        assert 'model === "POINTS_HOURS"' in self.TILES
        assert "t.boh_pool_cents" in self.TILES

    def test_each_model_has_its_own_tile_row(self):
        for token in ("PERCENT_TIPOUT", "POINTS_HOURS",
                      "t.pool_boh_cents", "t.boh_allocation_cents"):
            assert token in self.TILES, token

    def test_period_staff_table_branches_on_points_hours(self):
        period = APP_JS.split("async function renderPeriod(")[1].split(
            "\nasync function ")[0]
        assert 'p.model === "POINTS_HOURS"' in period
        # the hourly-pool sum must not be applied to a model that lacks the key
        poq = period.split('p.model === "POINTS_HOURS"')[1].split("} else {")[0]
        assert "s.boh_cents" not in poq
        assert "s.tips_cents + s.gratuity_cents" in poq   # a real take-home total


class TestTakeHomeStubs:
    """Pay-envelope slips printed from the export screen: one per employee,
    3 or 4 to a page with a cut line between. They are handed to staff, so
    the total must be the sum of the rows printed above it and the figures
    must come from the same payload the export report renders."""

    STUBS = APP_JS.split("async function renderPrintStubs(")[1].split(
        "\n/* ---------- audit log")[0]
    LINES = APP_JS.split("function stubLines(")[1].split("\n}")[0]

    def test_route_and_export_button(self):
        m = re.search(r"const routes = \{(.*?)\};", APP_JS, re.S)
        assert '"print-stubs": renderPrintStubs' in m.group(1)
        assert "#/print-stubs/${p.start}" in APP_JS
        assert "Take-home stubs" in APP_JS

    def test_reads_the_same_period_payload_as_the_report(self):
        # not a second endpoint with its own arithmetic: a stub that disagreed
        # with the summary printed beside it would be worse than no stub
        assert "/api/periods/${anchor}/export" in self.STUBS
        assert "reportScheme:" in self.STUBS       # honours the chosen scheme

    def test_total_is_the_sum_of_the_printed_rows(self):
        # the only total on the slip is derived from the same list that is
        # rendered, so it cannot drift from the rows above it
        assert "const total = lines.reduce((a, [, c]) => a + c, 0)" in self.STUBS
        assert 'el("td", {}, "Take home"), el("td", { class: "num" }, fmt(total))' \
            in self.STUBS

    def test_every_model_gets_rows(self):
        for token in ("PERCENT_TIPOUT", "POINTS_HOURS",
                      "s.boh_cents", "s.event_cents", "s.roundup_cents",
                      "Auto-gratuity (wages)"):
            assert token in self.LINES, token

    def test_zero_payout_employees_get_no_slip(self):
        assert "const paid = p.employees.filter(" in self.STUBS
        assert "> 0" in self.STUBS

    def test_draft_days_are_called_out(self):
        assert "not finalized and excluded" in self.STUBS

    def test_three_or_four_per_page_with_a_cut_line(self):
        assert "stubs per${per}" in self.STUBS
        assert "for (const n of [3, 4])" in self.STUBS
        assert "stubsPerPage" in self.STUBS      # the choice sticks
        assert "border-bottom: 1px dashed" in CSS
        assert ".stubs.per3 .stub:nth-child(3n)" in CSS
        assert ".stubs.per4 .stub:nth-child(4n)" in CSS
        # a slip must never be split across two sheets
        assert "page-break-inside: avoid" in CSS


class TestDateNavigationOnEveryDayScreen:
    """All three day screens navigate dates the same way (owner 2026-08-30).
    Tavern Law had only an invisible tap target on the date itself and
    La Fontana had only the button — neither is discoverable on its own, and
    a manager fixing last Tuesday should not have to go via the Period
    screen whichever venue they are in."""

    SCREENS = ("renderDay", "renderDayLF", "renderDayPoq")

    @staticmethod
    def block(name):
        i = APP_JS.index(f"function {name}(")
        m = re.search(r"\n(?:async )?function ", APP_JS[i + 10:])
        return APP_JS[i:i + 10 + m.start()] if m else APP_JS[i:]

    def test_every_day_screen_has_all_three_affordances(self):
        for name in self.SCREENS:
            blk = self.block(name)
            assert "shift(-1)" in blk and "shift(1)" in blk, f"{name}: arrows"
            assert "datePickButton(datePick)" in blk, f"{name}: picker button"
            assert "showPicker" in blk, f"{name}: tap the date"

    def test_the_picker_button_is_one_shared_helper(self):
        # so the three screens cannot drift apart again
        assert APP_JS.count("function datePickButton(") == 1
        # the trailing comma excludes the definition line, which also
        # contains the call-shaped substring
        assert APP_JS.count("datePickButton(datePick),") == 3

    def test_the_hidden_input_is_reachable_not_pointer_blocked(self):
        """A 0x0 or pointer-events:none input cannot be tapped, which is how
        La Fontana ended up without the affordance."""
        for name in self.SCREENS:
            blk = self.block(name)
            picker = blk[blk.index('type: "date"'):][:260]
            assert "pointer-events:none" not in picker, name
            assert "width:0" not in picker, name

    def test_navigating_keeps_you_on_the_day_route(self):
        for name in self.SCREENS:
            blk = self.block(name)
            assert "location.hash = `#/day/${datePick.value}`" in blk, name


class TestExportFeaturesAreVenueAgnostic:
    """Payroll entry, take-home stubs and the timecard backfill are the same
    job at every venue — a timecard is a timecard whatever the tip policy is
    (owner 2026-08-30). Only genuinely policy-specific things stay gated."""

    EXPORT = APP_JS.split("async function renderExport(")[1].split(
        "\n/* ---------- ")[0]

    def test_the_three_shared_buttons_are_not_model_gated(self):
        for label in ("Payroll entry sheet", "Take-home stubs",
                      "Fetch timecard data"):
            assert label in self.EXPORT, label
        # none of them sits inside a POINTS_HOURS branch
        poq_arms = self.EXPORT.split('p.model === "POINTS_HOURS"')
        for arm in poq_arms[1:]:
            head = arm[:400]
            for label in ("Payroll entry sheet", "Take-home stubs",
                          "Fetch timecard data"):
                assert label not in head, f"{label} still gated"

    def test_form_4070_stays_tip_out_only(self):
        """Not everything should be shared: Form 4070 is a tip-out venue's
        monthly IRS report and has no meaning elsewhere."""
        assert 'p.model === "PERCENT_TIPOUT" && p.scheme === "monthly"' in self.EXPORT
        assert "Form 4070" in self.EXPORT

    def test_paid_hours_tile_reaches_every_model(self):
        tiles = APP_JS.split("function poolTiles(")[1].split("\n}")[0]
        assert 'model !== "POINTS_HOURS" && t.paid_hours !== undefined' in tiles
        # and still refuses to show a number it cannot stand behind
        assert tiles.count("needs re-pull") >= 2
