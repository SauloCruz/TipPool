"""Pure extractors: raw Square API payloads -> day-input values + issues.

No network, no DB — everything here is unit-testable with fixture JSON.
Issues come in two severities:
  blocking — the affected field cannot be trusted (unmapped category or
             team member); the pull refuses to apply that field and the
             day cannot be finalized until resolved (never silently guess).
  warning  — surfaced to the manager but non-blocking (missing clock-out,
             all-zero declared cash tips, uncataloged line items).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from engine import Break, TippableWindow, clip_timecard

MONEY_FIELDS_FROM_SQUARE = (
    "food_sales_cents", "credit_tips_cents", "auto_gratuity_cents", "cash_tips_cents",
)

# Warning-severity codes a venue may mute (Setup screen). Blocking codes are
# deliberately NOT mutable — they gate finalize.
MUTABLE_WARNINGS = ("missing_clockout", "all_cash_tips_zero", "uncataloged_line_items")


def _amount(money: dict | None) -> int:
    return int(money["amount"]) if money else 0


def _iso(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


# ---------- catalog ----------

def build_catalog_lookup(batch: dict) -> dict[str, dict]:
    """variation_id -> {category_id, category_name, item_name} from a
    batch-retrieve response (objects=variations, related=items+categories)."""
    everything = {o["id"]: o for o in batch.get("objects", []) + batch.get("related_objects", [])}
    categories = {
        oid: o.get("category_data", {}).get("name", oid)
        for oid, o in everything.items() if o.get("type") == "CATEGORY"
    }
    lookup = {}
    for o in everything.values():
        if o.get("type") != "ITEM_VARIATION":
            continue
        item = everything.get(o.get("item_variation_data", {}).get("item_id", ""), {})
        idata = item.get("item_data", {})
        cat_id = (idata.get("reporting_category") or {}).get("id")
        if cat_id is None and idata.get("categories"):
            cat_id = idata["categories"][0].get("id")
        lookup[o["id"]] = {
            "category_id": cat_id,
            "category_name": categories.get(cat_id, cat_id),
            "item_name": idata.get("name", "?"),
        }
    return lookup


def extract_food_sales(orders: list[dict], catalog_lookup: dict[str, dict],
                       category_map: dict[str, dict]) -> dict:
    """Gross sales of line items whose category is mapped FOOD. Unmapped
    categories are blocking; line items with no catalog object (custom
    amounts) are a warning and not counted."""
    total = 0
    unmapped: dict[str, str] = {}
    uncataloged_cents = 0
    lines = []
    for order in orders:
        for li in order.get("line_items", []):
            gross = _amount(li.get("gross_sales_money"))
            var_id = li.get("catalog_object_id")
            info = catalog_lookup.get(var_id) if var_id else None
            if info is None or info["category_id"] is None:
                uncataloged_cents += gross
                continue
            entry = category_map.get(info["category_id"])
            group = entry.get("group") if entry else None
            if group is None:
                unmapped[info["category_id"]] = info["category_name"]
            elif group == "FOOD":
                total += gross
                lines.append({"item": info["item_name"], "gross_cents": gross,
                              "order_id": order.get("id")})
    issues = []
    if unmapped:
        issues.append({"severity": "blocking", "code": "unmapped_category",
                       "detail": unmapped, "blocks": ["food_sales_cents"]})
    if uncataloged_cents:
        issues.append({"severity": "warning", "code": "uncataloged_line_items",
                       "detail": {"gross_cents": uncataloged_cents}})
    return {"food_sales_cents": total, "issues": issues, "lines": lines}


# ---------- payments ----------

def extract_credit_tips(payments: list[dict]) -> dict:
    """Σ tip_money on COMPLETED card payments, net of refunded tips.
    Refund split rule: a refund eats the non-tip portion first, so the tip
    is considered refunded only for the part exceeding it:
        refunded_tip = clamp(refunded_total - (payment_total - tip), 0, tip)
    """
    total = 0
    rows = []
    for p in payments:
        if p.get("status") != "COMPLETED" or "card_details" not in p:
            continue
        tip = _amount(p.get("tip_money"))
        if tip == 0 and not p.get("refunded_money"):
            continue
        pay_total = _amount(p.get("total_money"))
        refunded = _amount(p.get("refunded_money"))
        refunded_tip = min(tip, max(0, refunded - (pay_total - tip)))
        net = tip - refunded_tip
        total += net
        rows.append({"payment_id": p.get("id"), "tip_cents": tip,
                     "refunded_tip_cents": refunded_tip})
    return {"credit_tips_cents": total, "payments": rows}


# ---------- service charges ----------

def extract_auto_gratuity(orders: list[dict], grat_cfg: dict) -> dict:
    """Order service charges owed to staff as auto-gratuity.

    Matching (any of): Square's explicit `type == AUTO_GRATUITY` (catalog
    gratuity charges carry no name on the order), the configured catalog id,
    or a case-insensitive name substring (custom/ad-hoc charges).

    Amount: `applied_money` — the pre-tax charge, which is what staff are
    owed and what the dashboard reports as Net Service Charges.
    `total_money` includes sales tax and must NOT be distributed."""
    want_id = grat_cfg.get("catalog_object_id")
    want_name = (grat_cfg.get("name_contains") or "").lower()
    total = 0
    rows = []
    for order in orders:
        for sc in order.get("service_charges", []):
            matched = (
                sc.get("type") == "AUTO_GRATUITY"
                or (want_id and sc.get("catalog_object_id") == want_id)
                or (want_name and want_name in sc.get("name", "").lower())
            )
            if not matched:
                continue
            applied = sc.get("applied_money")
            if applied is not None:
                amt = _amount(applied)
            else:  # older payloads: strip tax from the total
                amt = _amount(sc.get("total_money")) - _amount(sc.get("total_tax_money"))
            total += amt
            rows.append({"order_id": order.get("id"),
                         "name": sc.get("name") or sc.get("type") or "service charge",
                         "cents": amt})
    return {"auto_gratuity_cents": total, "charges": rows}


# ---------- timecards ----------

def extract_timecards(timecards: list[dict], emp_by_tmid: dict[str, dict],
                      business_day: date, windows: dict[int, TippableWindow],
                      tzname: str, increment: Decimal) -> dict:
    """One pull, three inputs (CLAUDE.md §3.4): FOH tippable hours, BOH
    worked roster, and cash tips = Σ declared_cash_tip_money over all
    non-manager timecards. EXCLUDED (manager) timecards are ignored
    entirely — including their declared tips.

    Hours (owner ruling 2026-07-05): still clipped to the tippable window,
    but exact within it — clock times are never rounded; minutes/60 is
    rounded to 2 decimals (increment 0.01), matching how Square displays
    hours. No quarter-hour rounding."""
    tz = ZoneInfo(tzname)
    window = windows[business_day.weekday()]
    w_start, w_end = window.bounds(business_day, tz)

    foh_hours: dict[str, float] = {}
    boh_worked: set[int] = set()
    cash = 0
    declared_any = False
    nonzero_declared = False
    unmapped: list[str] = []
    missing_clockout: list[str] = []
    cards = []

    for tc in timecards:
        tmid = tc.get("team_member_id", "?")
        emp = emp_by_tmid.get(tmid)
        if emp is None:
            unmapped.append(tmid)
            continue
        if emp["pool_role"] == "EXCLUDED":
            continue
        declared = _amount(tc.get("declared_cash_tip_money"))
        declared_any = True
        nonzero_declared = nonzero_declared or declared > 0
        cash += declared

        if emp["pool_role"] == "BOH":
            boh_worked.add(emp["id"])  # any BOH timecard counts, no clipping
            cards.append({"employee_id": emp["id"], "name": emp["display_name"],
                          "role": "BOH", "declared_cents": declared})
            continue

        # FOH: needs a complete shift to compute hours
        if not tc.get("end_at"):
            missing_clockout.append(emp["display_name"])
            cards.append({"employee_id": emp["id"], "name": emp["display_name"],
                          "role": "FOH", "declared_cents": declared,
                          "missing_clockout": True})
            continue
        breaks = [
            Break(_iso(b["start_at"]).astimezone(tz), _iso(b["end_at"]).astimezone(tz),
                  paid=bool(b.get("is_paid")))
            for b in tc.get("breaks", []) if b.get("end_at")
        ]
        clipped = clip_timecard(
            _iso(tc["start_at"]).astimezone(tz), _iso(tc["end_at"]).astimezone(tz),
            breaks, window_start=w_start, window_end=w_end,
            rounding_increment=increment,
        )
        key = str(emp["id"])
        tippable = float(clipped.tippable_hours)
        foh_hours[key] = round(foh_hours.get(key, 0.0) + tippable, 2)
        cards.append({"employee_id": emp["id"], "name": emp["display_name"],
                      "role": "FOH", "declared_cents": declared,
                      # raw = full shift as Square displays it (2 decimals)
                      "raw_hours": round(float(clipped.raw_hours), 2),
                      "tippable_hours": tippable})

    issues = []
    if unmapped:
        issues.append({"severity": "blocking", "code": "unmapped_team_member",
                       "detail": sorted(set(unmapped)),
                       "blocks": ["foh_hours", "boh_worked", "cash_tips_cents"]})
    if missing_clockout:
        issues.append({"severity": "warning", "code": "missing_clockout",
                       "detail": sorted(missing_clockout)})
    if declared_any and not nonzero_declared:
        issues.append({"severity": "warning", "code": "all_cash_tips_zero",
                       "detail": "every declared cash tip is $0 — possible skipped declarations"})

    return {
        "foh_hours": {k: foh_hours[k] for k in sorted(foh_hours, key=int)},
        "boh_worked": sorted(boh_worked),
        "cash_tips_cents": cash,
        "issues": issues,
        "timecards": cards,
    }


# ---------- PERCENT_TIPOUT (La Fontana, M5) ----------

# job-title keywords -> LF role, for the assigned-role-wins mismatch warning
_TITLE_ROLE_HINTS = (
    ("server", "SERVER"), ("waiter", "SERVER"), ("waitress", "SERVER"),
    ("bus", "BUSSER"), ("runner", "BUSSER"),
    ("host", "HOST"),
    ("cook", "BOH"), ("chef", "BOH"), ("kitchen", "BOH"), ("dish", "BOH"),
)


def _role_from_title(title: str | None) -> str | None:
    t = (title or "").lower()
    for kw, role in _TITLE_ROLE_HINTS:
        if kw in t:
            return role
    return None


def _refund_net_tip(p: dict) -> int:
    """Same refund-split rule as TL: a refund eats the non-tip portion first."""
    tip = _amount(p.get("tip_money"))
    if tip == 0:
        return 0
    pay_total = _amount(p.get("total_money"))
    refunded = _amount(p.get("refunded_money"))
    return tip - min(tip, max(0, refunded - (pay_total - tip)))


def extract_server_tips(payments: list[dict], emp_by_tmid: dict[str, dict]) -> dict:
    """Per-server card-tip attribution via payment.team_member_id (M5 §4).

    - Attributed to a mapped SERVER: counts for that server.
    - No team member on the payment (counter sale, house account), or
      attributed to a non-server (host rang it) or an EXCLUDED manager:
      lands in the UNATTRIBUTED bucket — surfaced to the manager, who must
      assign or mark house before finalize. Never silently assigned.
    - Attributed to an unmapped team member id: BLOCKING (map them first).
    """
    per_server: dict[str, int] = {}
    unattributed = 0
    unmapped: set[str] = set()
    rows = []
    for p in payments:
        if p.get("status") != "COMPLETED" or "card_details" not in p:
            continue
        net = _refund_net_tip(p)
        if net == 0:
            continue
        tmid = p.get("team_member_id")
        emp = emp_by_tmid.get(tmid) if tmid else None
        if tmid and emp is None:
            unmapped.add(tmid)
            continue
        if emp is not None and emp["pool_role"] == "SERVER":
            key = str(emp["id"])
            per_server[key] = per_server.get(key, 0) + net
            rows.append({"payment_id": p.get("id"), "tip_cents": net,
                         "server": emp["display_name"]})
        else:
            reason = ("no team member" if emp is None
                      else f"attributed to {emp['display_name']} ({emp['pool_role']})")
            unattributed += net
            rows.append({"payment_id": p.get("id"), "tip_cents": net,
                         "unattributed": reason})
    issues = []
    if unmapped:
        issues.append({"severity": "blocking", "code": "unmapped_team_member",
                       "detail": sorted(unmapped),
                       "blocks": ["server_tips", "server_cash_tips", "hours",
                                  "unattributed_tips_cents"]})
    if unattributed > 0:
        issues.append({"severity": "warning", "code": "unattributed_tips",
                       "detail": {"cents": unattributed}})
    return {"server_tips": {k: per_server[k] for k in sorted(per_server, key=int)},
            "unattributed_tips_cents": unattributed,
            "issues": issues, "payments": rows}


def extract_lf_timecards(timecards: list[dict], emp_by_tmid: dict[str, dict]) -> dict:
    """LF timecards: full-shift hours (minus unpaid breaks, exact minutes,
    2-decimal display, NO window clipping per the M5 ruling), declared cash
    tips per SERVER, and the assigned-role-wins job-title mismatch warning."""
    hours: dict[str, float] = {}
    server_cash: dict[str, int] = {}
    unmapped: list[str] = []
    missing_clockout: list[str] = []
    mismatches: list[str] = []
    server_seen = False
    any_declared = False
    cards = []
    for tc in timecards:
        tmid = tc.get("team_member_id", "?")
        emp = emp_by_tmid.get(tmid)
        if emp is None:
            unmapped.append(tmid)
            continue
        if emp["pool_role"] == "EXCLUDED":
            continue
        title = (tc.get("wage") or {}).get("title")
        hinted = _role_from_title(title)
        if hinted and hinted != emp["pool_role"]:
            mismatches.append(
                f"{emp['display_name']}: timecard job {title!r} looks like"
                f" {hinted}, assigned role {emp['pool_role']} wins")
        declared = _amount(tc.get("declared_cash_tip_money"))
        if emp["pool_role"] == "SERVER":
            server_seen = True
            any_declared = any_declared or declared > 0
            if declared:
                key = str(emp["id"])
                server_cash[key] = server_cash.get(key, 0) + declared
        if not tc.get("end_at"):
            missing_clockout.append(emp["display_name"])
            cards.append({"employee_id": emp["id"], "name": emp["display_name"],
                          "role": emp["pool_role"], "declared_cents": declared,
                          "missing_clockout": True})
            continue
        start = _iso(tc["start_at"]).timestamp()
        end = _iso(tc["end_at"]).timestamp()
        seconds = end - start
        for b in tc.get("breaks", []):
            if b.get("is_paid") or not b.get("end_at"):
                continue
            b0, b1 = _iso(b["start_at"]).timestamp(), _iso(b["end_at"]).timestamp()
            seconds -= max(0.0, min(b1, end) - max(b0, start))
        worked = float((Decimal(round(seconds)) / 3600).quantize(Decimal("0.01")))
        key = str(emp["id"])
        hours[key] = round(hours.get(key, 0.0) + worked, 2)
        cards.append({"employee_id": emp["id"], "name": emp["display_name"],
                      "role": emp["pool_role"], "declared_cents": declared,
                      "worked_hours": worked, "job_title": title})
    issues = []
    if unmapped:
        issues.append({"severity": "blocking", "code": "unmapped_team_member",
                       "detail": sorted(set(unmapped)),
                       "blocks": ["server_tips", "server_cash_tips", "hours",
                                  "unattributed_tips_cents"]})
    if missing_clockout:
        issues.append({"severity": "warning", "code": "missing_clockout",
                       "detail": sorted(missing_clockout)})
    if mismatches:
        issues.append({"severity": "warning", "code": "role_mismatch",
                       "detail": mismatches})
    if server_seen and not any_declared:
        issues.append({"severity": "warning", "code": "all_cash_tips_zero",
                       "detail": "every server's declared cash tip is $0 — "
                                 "expected until the declaration policy starts"})
    return {"hours": {k: hours[k] for k in sorted(hours, key=int)},
            "server_cash_tips": {k: server_cash[k] for k in sorted(server_cash, key=int)},
            "issues": issues, "timecards": cards}
