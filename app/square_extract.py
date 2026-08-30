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

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from zoneinfo import ZoneInfo

from engine import Break, TippableWindow, clip_timecard, round_hours_up

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


# ---------- private events: POOL_HOURS (Tavern Law) ----------

def extract_event_items(orders: list[dict], catalog_lookup: dict[str, dict],
                        category_map: dict[str, dict],
                        cfg: dict | None = None) -> dict:
    """Private-event line items, told apart from ordinary trade by their
    Square category (group EVENT).

    Tavern Law rings a contracted event as catalog items rather than under a
    separate logon: `Event Food Packages`, `Event Beverage Package`,
    `Event Room Fee`, `Event Taxes`, and — days or weeks ahead of the night
    itself — an `Event Deposit`, which is the party's gratuity.

    Three things come out of that (owner 2026-08-29):

    * `event_food_cents` — Σ **every** line whose name contains the configured
      food word. There is regularly more than one: 8/22 rang $440 and $75, and
      8/07's second $60 line was missed by hand.
    * `deposits` — every deposit line found, with its note and receipt, for
      the day screen to offer. They are NOT counted here: a deposit belongs to
      the event it was taken for, which is a different day, and only a human
      can say which. See `tl_deposit_lookback_days`.
    * `other_cents` / `other_lines` — beverage packages, room fees, event
      taxes. Out of the pool and out of food sales, but always reported: money
      that vanishes silently is as bad as money wrongly paid out.

    None of it reaches food sales — the EVENT category is not FOOD, so
    `extract_food_sales` already leaves it alone.
    """
    cfg = cfg or {}
    food_word = (cfg.get("food_contains") or "food").lower()
    deposit_word = (cfg.get("deposit_contains") or "deposit").lower()

    food_cents = 0
    other_cents = 0
    food_lines, other_lines, deposits = [], [], []
    order_ids: set[str] = set()
    for order in orders:
        for li in order.get("line_items", []):
            var_id = li.get("catalog_object_id")
            info = catalog_lookup.get(var_id) if var_id else None
            if info is None or info["category_id"] is None:
                continue
            entry = category_map.get(info["category_id"])
            if (entry or {}).get("group") != "EVENT":
                continue
            gross = _amount(li.get("gross_sales_money"))
            name = info["item_name"] or li.get("name") or "?"
            row = {"item": name, "gross_cents": gross,
                   "order_id": order.get("id"), "note": li.get("note")}
            order_ids.add(order.get("id"))
            if deposit_word in name.lower():
                tender = next(iter(order.get("tenders") or []), {})
                deposits.append({**row,
                                 "deposit_id": f"{order.get('id')}:{li.get('uid')}",
                                 "receipt": (tender.get("id") or "")[:4],
                                 "created_at": order.get("created_at"),
                                 "note": li.get("note") or tender.get("note")})
            elif food_word in name.lower():
                food_cents += gross
                food_lines.append(row)
            else:
                other_cents += gross
                other_lines.append(row)

    return {"event_food_cents": food_cents, "event_food_lines": food_lines,
            "other_cents": other_cents, "other_lines": other_lines,
            "deposits": deposits, "order_ids": sorted(order_ids)}


def extract_event_tips(orders: list[dict], payments: list[dict],
                       event_order_ids: Iterable[str],
                       grat_cfg: dict | None = None,
                       house_names: Iterable[str] = ()) -> dict:
    """Tip money riding on an event ticket: card tips and gratuity service
    charges on any order that carried an event line item.

    Owner ruling 2026-08-29 — "any other Event Tip should roll into Event
    Tips". The 8/22 event's second ticket carried a $55.80 auto-gratuity that
    would otherwise have been split across every bartender on the floor
    instead of the crew who worked the party. Refunds net off the same way
    they do everywhere else: the refund eats the ordinary part of the check
    first, so only the excess reaches the tip.
    """
    ids = set(event_order_ids or ())
    want_id = (grat_cfg or {}).get("catalog_object_id")
    want_name = (grat_cfg or {}).get("name_contains")

    pays_by_order: dict[str, list[dict]] = {}
    for p in payments or ():
        if p.get("status") == "COMPLETED":
            pays_by_order.setdefault(p.get("order_id"), []).append(p)

    total = 0
    rows = []
    for order in orders:
        oid = order.get("id")
        if oid not in ids:
            continue
        pays = pays_by_order.get(oid, [])
        paid = sum(_amount(p.get("total_money")) for p in pays)
        refunded = sum(_amount(p.get("refunded_money")) for p in pays)
        sc = sum(_charge_cents(c) for c in order.get("service_charges", [])
                 if _is_staff_gratuity(c, want_id, want_name, house_names))
        tip = sum(_amount(p.get("tip_money")) for p in pays)
        pool = sc + tip
        if not pool:
            continue
        back = min(pool, max(0, refunded - (paid - pool))) if refunded else 0
        total += pool - back
        rows.append({"order_id": oid, "service_charge_cents": sc,
                     "tips_cents": tip, "refunded_cents": back})
    return {"event_tips_cents": total, "lines": rows}


# ---------- payments ----------

def extract_credit_tips(payments: list[dict],
                        exclude_order_ids: Iterable[str] = ()) -> dict:
    """Σ tip_money on COMPLETED card payments, net of refunded tips.
    Refund split rule: a refund eats the non-tip portion first, so the tip
    is considered refunded only for the part exceeding it:
        refunded_tip = clamp(refunded_total - (payment_total - tip), 0, tip)

    `exclude_order_ids` drops payments belonging to a private event: that
    money is the event pool's, not the daily pool's (see extract_event_money).
    """
    skip = set(exclude_order_ids or ())
    total = 0
    rows = []
    for p in payments:
        if p.get("status") != "COMPLETED" or "card_details" not in p:
            continue
        if p.get("order_id") in skip:
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

def _is_house_charge(sc: dict, house_names: Iterable[str] = ()) -> bool:
    """A charge the house levies for its own account, never staff money.

    Checked FIRST and it wins outright, because the alternative is not safe:
    Square types a service charge `AUTO_GRATUITY` whenever it is flagged as
    gratuity in the dashboard, and that flag is set by whoever created the
    charge — so the Poquitos 3% "Event Administrative Fee" could arrive
    carrying the same type as a real tip and be pooled before its name was
    ever looked at. Naming the house's charges explicitly is the only test
    that does not depend on how someone ticked a box in Square.
    """
    name = (sc.get("name") or "").lower()
    return any(h and h.lower() in name for h in house_names)


def _name_matchers(want_name) -> list[str]:
    """Normalise the configured name match into a list of substrings.

    Accepts a list, or a comma-separated string, because one venue's
    gratuity arrives under several names: Tavern Law rings some nights as a
    catalog AUTO_GRATUITY and others as a CUSTOM charge literally called
    "Service Charge".
    """
    if want_name is None:
        return []
    parts = want_name if isinstance(want_name, (list, tuple)) else str(want_name).split(",")
    return [p.strip().lower() for p in parts if str(p).strip()]


def _is_staff_gratuity(sc: dict, want_id: str | None, want_name,
                       house_names: Iterable[str] = ()) -> bool:
    """Is this service charge money owed to staff?

    Square's explicit `AUTO_GRATUITY` type (catalog gratuity charges carry no
    name on the order), the configured catalog id, or a case-insensitive name
    match for custom/ad-hoc charges — unless it is a named house charge, which
    is never staff money whatever type it carries.
    """
    if _is_house_charge(sc, house_names):
        return False
    name = (sc.get("name") or "").lower()
    return bool(
        sc.get("type") == "AUTO_GRATUITY"
        or (want_id and sc.get("catalog_object_id") == want_id)
        or any(w in name for w in _name_matchers(want_name))
    )


def _charge_cents(sc: dict) -> int:
    applied = sc.get("applied_money")
    if applied is not None:
        return _amount(applied)
    return _amount(sc.get("total_money")) - _amount(sc.get("total_tax_money"))


def extract_auto_gratuity(orders: list[dict], grat_cfg: dict,
                         payments: list[dict] | None = None,
                         exclude_order_ids: Iterable[str] = (),
                         house_names: Iterable[str] = ()) -> dict:
    """Order service charges owed to staff as auto-gratuity.

    Matching (any of): Square's explicit `type == AUTO_GRATUITY` (catalog
    gratuity charges carry no name on the order), the configured catalog id,
    or a case-insensitive name substring (custom/ad-hoc charges).

    Amount: `applied_money` — the pre-tax charge, which is what staff are
    owed and what the dashboard reports as Net Service Charges.
    `total_money` includes sales tax and must NOT be distributed."""
    want_id = grat_cfg.get("catalog_object_id")
    want_name = grat_cfg.get("name_contains")
    # A private event's 20% charge is also an AUTO_GRATUITY, but it belongs to
    # the event pool rather than the day's — the caller passes those order ids
    # here so the same money is never distributed twice.
    skip = set(exclude_order_ids or ())

    # Refunds: money handed back is not owed to staff. Same split rule as
    # tips — a refund eats the non-service-charge portion of the check first,
    # so the charge is only refunded for the part exceeding it. A fully
    # refunded check therefore returns its whole gratuity; a small partial
    # refund returns none of it. (Matches Square's own Net Service Charges.)
    refunded_by_order: dict[str, int] = {}
    paid_by_order: dict[str, int] = {}
    for p in payments or ():
        oid = p.get("order_id")
        if not oid:
            continue
        refunded_by_order[oid] = refunded_by_order.get(oid, 0) + _amount(p.get("refunded_money"))
        paid_by_order[oid] = paid_by_order.get(oid, 0) + _amount(p.get("total_money"))

    total = 0
    refunded_total = 0
    rows = []
    held: list[dict] = []
    for order in orders:
        if order.get("id") in skip:
            continue
        for sc in order.get("service_charges", []):
            if not _is_staff_gratuity(sc, want_id, want_name, house_names):
                # Out of the pool — but say so. A charge the house keeps is
                # routine; anything else is money nobody has accounted for.
                # Tavern Law rang $59.80 on 2026-08-05 as a CUSTOM charge
                # named "Service Charge", which matched nothing and vanished
                # from the day while the spreadsheet paid it out.
                cents = _charge_cents(sc)
                if cents:
                    held.append({
                        "order_id": order.get("id"),
                        "name": sc.get("name") or sc.get("type") or "service charge",
                        "cents": cents,
                        "house": _is_house_charge(sc, house_names)})
                continue
            amt = _charge_cents(sc)
            oid = order.get("id")
            refunded = refunded_by_order.get(oid, 0)
            back = 0
            if refunded:
                paid = paid_by_order.get(oid, 0)
                back = min(amt, max(0, refunded - (paid - amt)))
            total += amt - back
            refunded_total += back
            rows.append({"order_id": oid,
                         "name": sc.get("name") or sc.get("type") or "service charge",
                         "cents": amt - back, "refunded_cents": back})
    issues = []
    unrecognised = [h for h in held if not h["house"]]
    if unrecognised:
        issues.append({
            "severity": "warning", "code": "unmatched_service_charge",
            "detail": {"cents": sum(h["cents"] for h in unrecognised),
                       "names": sorted({h["name"] for h in unrecognised})}})
    return {"auto_gratuity_cents": total, "charges": rows,
            "refunded_gratuity_cents": refunded_total,
            "held_out": held, "issues": issues}


# ---------- private events ----------

def extract_event_money(orders: list[dict], payments: list[dict],
                        event_tmid: str, tz_name: str,
                        grat_cfg: dict | None = None,
                        house_names: Iterable[str] = ()) -> dict:
    """Private-event money, told apart from the day's ordinary trade by the
    Square logon that rang it.

    Poquitos rings a contracted event under a shared "Event Host" pin (owner
    2026-08-28), so every order that account created belongs to the event and
    none of it belongs to the daily pool: the 20% service charge is the event
    pool, not auto-gratuity, and any card tips are event tips, not credit
    tips. Ordinary large-party gratuity is untouched — including
    private-dining-room tickets, which regular servers ring under their own
    logon and which are NOT events (owner 2026-08-28).

    Two other things come out of the same orders:

    Only service charges owed to STAFF join the pool — matched the same way
    the daily auto-gratuity is. The policy's 3% administrative fee goes to the
    manager who organised the event and "never touches the staff pool", so a
    charge on the event ticket that is not a gratuity is held out and reported
    in `other_charges` for the day to flag. Silently pooling it would overpay
    everyone; silently dropping it would hide money.

    * `card_cents` — how much of the pool the card processor actually handled,
      so the fee is withheld from that part only. The 08/17 event was tendered
      EXTERNAL (invoiced), where a blanket fee would have been simply wrong.
    * `window` — the event's clock times, inferred as the top of the hour
      before the first order was opened through the moment the ticket was
      paid (owner 2026-08-28). It decides how many of the bartender's hours
      were worked on the event.
    """
    empty = {"event_service_charge_cents": 0, "event_tips_cents": 0,
             "card_cents": 0, "order_ids": [], "window": None, "orders": [],
             "other_charges": []}
    if not event_tmid:
        return dict(empty)

    ev_orders = [o for o in orders
                 if o.get("created_by_team_member_id") == event_tmid]
    order_ids = [o["id"] for o in ev_orders if o.get("id")]
    if not ev_orders:
        return dict(empty)

    want_id = (grat_cfg or {}).get("catalog_object_id")
    want_name = (grat_cfg or {}).get("name_contains")

    pays_by_order: dict[str, list[dict]] = {}
    for p in payments or ():
        pays_by_order.setdefault(p.get("order_id"), []).append(p)

    sc_total = tip_total = card_base = 0
    rows = []
    other: list[dict] = []
    for o in ev_orders:
        oid = o.get("id")
        pays = [p for p in pays_by_order.get(oid, [])
                if p.get("status") == "COMPLETED"]
        paid = sum(_amount(p.get("total_money")) for p in pays)
        refunded = sum(_amount(p.get("refunded_money")) for p in pays)
        card_paid = sum(_amount(p.get("total_money")) for p in pays
                        if "card_details" in p)

        sc = 0
        for c in o.get("service_charges", []):
            if _is_staff_gratuity(c, want_id, want_name, house_names):
                sc += _charge_cents(c)
            elif _charge_cents(c):
                # out of the pool either way, but say which kind: a named house
                # charge (the 3% admin fee) is routine, an unrecognised one is
                # money nobody has accounted for and wants a human look
                other.append({"order_id": oid,
                              "name": c.get("name") or c.get("type") or "service charge",
                              "percentage": c.get("percentage"),
                              "cents": _charge_cents(c),
                              "house": _is_house_charge(c, house_names)})
        tip = sum(_amount(p.get("tip_money")) for p in pays)

        # Refunds come off the same way as elsewhere: a refund eats the
        # ordinary part of the check first, so only the excess reaches the
        # charge and the tip.
        pool = sc + tip
        back = min(pool, max(0, refunded - (paid - pool))) if refunded else 0
        # split the clawback across the two lines in proportion
        sc_back = (back * sc) // pool if pool else 0
        tip_back = back - sc_back

        sc_total += sc - sc_back
        tip_total += tip - tip_back
        # the processor only handled the card-tendered share of this check
        if paid:
            card_base += ((sc - sc_back) + (tip - tip_back)) * card_paid // paid
        rows.append({"order_id": oid, "ticket_name": o.get("ticket_name"),
                     "service_charge_cents": sc - sc_back,
                     "tips_cents": tip - tip_back,
                     "refunded_cents": back,
                     "tenders": sorted({p.get("source_type") for p in pays}),
                     "created_at": o.get("created_at"),
                     "closed_at": o.get("closed_at")})

    tz = ZoneInfo(tz_name)
    opened = min(_iso(o["created_at"]) for o in ev_orders if o.get("created_at"))
    paid_at = max(_iso(o["closed_at"]) for o in ev_orders if o.get("closed_at"))
    start = opened.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    end = paid_at.astimezone(tz)
    if end <= start:                      # a same-hour event: give it the hour
        end = start + timedelta(hours=1)
    return {"event_service_charge_cents": sc_total,
            "event_tips_cents": tip_total,
            "card_cents": card_base,
            "order_ids": order_ids,
            "window": {"start_at": start.isoformat(), "end_at": end.isoformat()},
            "orders": rows, "other_charges": other}


# ---------- timecards ----------

def extract_timecards(timecards: list[dict], emp_by_tmid: dict[str, dict],
                      business_day: date, windows: dict[int, TippableWindow],
                      tzname: str, increment: Decimal,
                      job_roles: dict[str, str] | None = None,
                      door_weight: Fraction = Fraction(1, 2),
                      min_shift_minutes: float = 15) -> dict:
    """One pull, three inputs (CLAUDE.md §3.4): FOH tippable hours, BOH
    worked roster, and cash tips.

    Role comes from the timecard's Square job (`wage.title`), not from the
    person (owner 2026-08-29): staff hold two jobs — a bartender who also
    manages, a server who also hosts — and the job chosen at clock-in is what
    the shift is worth. A `DOOR` job earns `door_weight` of an hour's tip
    credit per hour worked; a person who works both a floor shift and a door
    shift the same day gets a blended weight, which is exactly their credited
    hours over their raw hours. An EXCLUDED job earns nothing at all.

    Cash tips = Σ `declared_cash_tip_money` over **every** timecard, EXCLUDED
    jobs included: an excluded person's hours earn nothing, but tips they
    collected cannot be kept and go into the pool (owner 2026-08-29).

    Hours: clipped to the tippable window, then rounded UP to the next
    `increment` (0.05 h — owner ruling 2026-07-29). Clock times themselves are
    never rounded. Timecards that can't yield a duration (no clock-out, or a
    clock-out not after the clock-in) are reported as issues, never raised:
    one bad punch must not fail the whole day's pull."""
    tz = ZoneInfo(tzname)
    window = windows[business_day.weekday()]
    w_start, w_end = window.bounds(business_day, tz)
    roles = dict(job_roles or {})

    # Seconds, not hours: the 0.05 round-up is applied ONCE per person per
    # day, at the end. Rounding each timecard first pays a split punch more
    # than an unbroken shift — Jacob Ruley's 2026-08-13 was 17:00-22:46 then
    # 22:46-00:36, exactly 7.00 h, but read as 5.80 + 1.25 = 7.05.
    secs: dict[str, int] = {}
    # per employee: Σ seconds × the weight of the job each was worked under
    credited: dict[str, Fraction] = {}
    boh_worked: set[int] = set()
    cash = 0
    declared_any = False
    nonzero_declared = False
    unmapped: list[str] = []
    unmapped_jobs: list[str] = []
    role_mismatch: list[str] = []
    short_shift: list[str] = []
    missing_clockout: list[str] = []
    bad_interval: list[str] = []
    cards = []

    for tc in timecards:
        tmid = tc.get("team_member_id", "?")
        emp = emp_by_tmid.get(tmid)
        if emp is None:
            unmapped.append(tmid)
            continue
        title = (tc.get("wage") or {}).get("title")
        role = roles.get(title)
        if role is None:
            unmapped_jobs.append(title or "(no job title)")
            continue

        declared = _amount(tc.get("declared_cash_tip_money"))
        declared_any = True
        nonzero_declared = nonzero_declared or declared > 0
        cash += declared

        if role == "EXCLUDED":
            # Hours earn nothing by construction, but the cash above is
            # pooled and the shift is still shown so the day explains itself.
            cards.append({"employee_id": emp["id"], "name": emp["display_name"],
                          "role": "EXCLUDED", "job_title": title,
                          "declared_cents": declared})
            continue

        if role == "BOH":
            boh_worked.add(emp["id"])  # any BOH timecard counts, no clipping
            # ...but say so when the punch is too short to be a shift. Jose
            # Medina's 2026-08-01 kitchen "shift" was 17:00-17:01, and it put
            # him on the roster, splitting the allocation four ways instead of
            # three. The roster still includes him — only the manager may take
            # someone off it — but the day now names the punch.
            mins = None
            if tc.get("end_at"):
                mins = (_iso(tc["end_at"]) - _iso(tc["start_at"])).total_seconds() / 60
                if 0 <= mins < min_shift_minutes:
                    short_shift.append(
                        f"{emp['display_name']} ({round(mins)} min)")
            cards.append({"employee_id": emp["id"], "name": emp["display_name"],
                          "role": "BOH", "job_title": title,
                          "declared_cents": declared,
                          **({"minutes": round(mins)} if mins is not None else {})})
            if emp["pool_role"] == "FOH":
                role_mismatch.append(
                    f"{emp['display_name']} worked {title} (kitchen) but is "
                    f"filed as front of house")
            continue

        # FOH / DOOR: needs a complete shift to compute hours
        if emp["pool_role"] == "BOH":
            role_mismatch.append(
                f"{emp['display_name']} worked {title} (front of house) but "
                f"is filed as kitchen")
        weight = door_weight if role == "DOOR" else Fraction(1)
        if not tc.get("end_at"):
            missing_clockout.append(emp["display_name"])
            cards.append({"employee_id": emp["id"], "name": emp["display_name"],
                          "role": role, "job_title": title,
                          "declared_cents": declared, "missing_clockout": True})
            continue
        # A clock-out that isn't after the clock-in (a same-minute double
        # punch, or a backwards manual edit in Square) has no duration to
        # clip. Report it and move on — one bad punch must never fail the
        # whole day's pull.
        t_in, t_out = _iso(tc["start_at"]), _iso(tc["end_at"])
        if t_out <= t_in:
            local_in = t_in.astimezone(tz).strftime("%H:%M")
            local_out = t_out.astimezone(tz).strftime("%H:%M")
            bad_interval.append(f"{emp['display_name']} ({local_in} → {local_out})")
            cards.append({"employee_id": emp["id"], "name": emp["display_name"],
                          "role": role, "job_title": title,
                          "declared_cents": declared,
                          "invalid_interval": True,
                          "raw_hours": 0.0, "tippable_hours": 0.0,
                          "start_at": tc["start_at"], "end_at": tc["end_at"],
                          "rate_cents": _rate_cents(tc)})
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
        secs[key] = secs.get(key, 0) + clipped.tippable_seconds
        credited[key] = credited.get(key, Fraction(0)) + (
            Fraction(clipped.tippable_seconds) * weight)
        cards.append({"employee_id": emp["id"], "name": emp["display_name"],
                      "role": role, "job_title": title,
                      "declared_cents": declared,
                      # raw = full shift as Square displays it (2 decimals)
                      "raw_hours": round(float(clipped.raw_hours), 2),
                      # this shift's own clipped hours, UNROUNDED — the day's
                      # credited total is rounded once, below, so these rows
                      # need not sum to it exactly
                      "tippable_hours": round(clipped.tippable_seconds / 3600, 2),
                      "credited_hours": round(
                          float(Fraction(clipped.tippable_seconds) * weight) / 3600, 2),
                      # clock times and the job's rate, so period reports can
                      # split a shift at midnight and price paid hours the way
                      # payroll does — none of this touches the tip pool
                      "start_at": tc["start_at"], "end_at": tc["end_at"],
                      "rate_cents": _rate_cents(tc)})

    # One round-up per person per day (owner 2026-07-29: credited hours step
    # in 0.05 and always round UP).
    foh_hours = {k: float(round_hours_up(Decimal(v) / 3600, increment))
                 for k, v in secs.items()}

    # An employee's tip credit per hour: 1 for a floor-only day, door_weight
    # for a door-only day, and the blend in between for a split night. The
    # engine multiplies reported hours by this, so it reproduces credited
    # hours while the day screen still shows the hours actually worked.
    role_weights = {}
    for key, worked in secs.items():
        if worked > 0 and credited.get(key, Fraction(0)) != Fraction(worked):
            role_weights[key] = credited[key] / Fraction(worked)

    issues = []
    if unmapped:
        issues.append({"severity": "blocking", "code": "unmapped_team_member",
                       "detail": sorted(set(unmapped)),
                       "blocks": ["foh_hours", "boh_worked", "cash_tips_cents",
                                  "foh_role_weights"]})
    if unmapped_jobs:
        issues.append({"severity": "blocking", "code": "unmapped_job_title",
                       "detail": sorted(set(unmapped_jobs)),
                       "blocks": ["foh_hours", "boh_worked", "cash_tips_cents",
                                  "foh_role_weights"]})
    if role_mismatch:
        issues.append({"severity": "warning", "code": "job_role_mismatch",
                       "detail": sorted(set(role_mismatch))})
    if short_shift:
        issues.append({"severity": "warning", "code": "short_kitchen_shift",
                       "detail": sorted(set(short_shift))})
    if missing_clockout:
        issues.append({"severity": "warning", "code": "missing_clockout",
                       "detail": sorted(missing_clockout)})
    if bad_interval:
        issues.append({"severity": "warning", "code": "invalid_timecard",
                       "detail": sorted(bad_interval)})
    if declared_any and not nonzero_declared:
        issues.append({"severity": "warning", "code": "all_cash_tips_zero",
                       "detail": "every declared cash tip is $0 — possible skipped declarations"})

    return {
        "foh_hours": {k: foh_hours[k] for k in sorted(foh_hours, key=int)},
        "foh_role_weights": {k: str(role_weights[k])
                             for k in sorted(role_weights, key=int)},
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


def _rate_cents(tc: dict) -> int | None:
    """The hourly rate Square recorded for the job chosen at clock-in."""
    return ((tc.get("wage") or {}).get("hourly_rate") or {}).get("amount")


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
    bad_interval: list[str] = []
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
        # Same guard as the POOL_HOURS path: a clock-out not after the
        # clock-in has no duration. Without this a backwards punch would
        # quietly subtract negative hours from the person's total.
        if end <= start:
            tzi = _iso(tc["start_at"]).tzinfo
            bad_interval.append(
                f"{emp['display_name']} "
                f"({_iso(tc['start_at']).strftime('%H:%M')} → "
                f"{_iso(tc['end_at']).astimezone(tzi).strftime('%H:%M')})")
            cards.append({"employee_id": emp["id"], "name": emp["display_name"],
                          "role": emp["pool_role"], "declared_cents": declared,
                          "invalid_interval": True, "worked_hours": 0.0,
                          "job_title": title,
                          "start_at": tc["start_at"], "end_at": tc["end_at"],
                          "rate_cents": _rate_cents(tc)})
            continue
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
                      "worked_hours": worked, "job_title": title,
                      # as above: reporting only, never part of a payout
                      "start_at": tc["start_at"], "end_at": tc["end_at"],
                      "rate_cents": _rate_cents(tc)})
    issues = []
    if unmapped:
        issues.append({"severity": "blocking", "code": "unmapped_team_member",
                       "detail": sorted(set(unmapped)),
                       "blocks": ["server_tips", "server_cash_tips", "hours",
                                  "unattributed_tips_cents"]})
    if missing_clockout:
        issues.append({"severity": "warning", "code": "missing_clockout",
                       "detail": sorted(missing_clockout)})
    if bad_interval:
        issues.append({"severity": "warning", "code": "invalid_timecard",
                       "detail": sorted(bad_interval)})
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


# ---------- timecards: POINTS_HOURS (Poquitos, M6) ----------

def extract_timecards_poq(timecards: list[dict], emp_by_tmid: dict[str, dict],
                          tzname: str, increment: Decimal,
                          job_roles: dict[str, str],
                          ignore_tmids: Iterable[str] = ()) -> dict:
    # NOTE `increment` is accepted for signature parity with the other
    # extractors but deliberately unused: Poquitos does not round hours to an
    # increment (owner 2026-08-14). See the quantize below.
    """Turn a day's timecards into POINTS_HOURS shifts.

    Role is read from each timecard's Square job (`wage.title`) — the job the
    employee chose at clock-in — so multi-role staff and split nights are
    credited correctly without any manual marking (owner ruling 2026-08-03).

    Unlike Tavern Law there is NO tippable-window clipping: the Poquitos
    policy pays on "the total number of hours worked". Unpaid breaks are still
    deducted, and the total is rounded up to the venue's increment.

    A job title with no mapping is BLOCKING — the day cannot be trusted until
    someone says what it is worth (never guess a point value).

    `ignore_tmids` drops timecards belonging to a shared till account — the
    "Event Host" logon is a pin for ringing an event, not a person (owner
    2026-08-28), and paying its clocked hours would hand a real person's
    share to nobody.
    """
    skip_tmids = set(ignore_tmids or ())
    tz = ZoneInfo(tzname)
    shifts: list[dict] = []
    cash = 0
    declared_any = False
    nonzero_declared = False
    unmapped_members: list[str] = []
    unmapped_jobs: list[str] = []
    missing_clockout: list[str] = []
    bad_interval: list[str] = []

    for tc in timecards:
        tmid = tc.get("team_member_id", "?")
        if tmid in skip_tmids:
            continue
        emp = emp_by_tmid.get(tmid)
        if emp is None:
            unmapped_members.append(tmid)
            continue
        title = (tc.get("wage") or {}).get("title")
        role = job_roles.get(title)
        if role is None:
            unmapped_jobs.append(title or "(no job title)")
            continue

        declared = _amount(tc.get("declared_cash_tip_money"))
        declared_any = True
        nonzero_declared = nonzero_declared or declared > 0
        cash += declared

        if not tc.get("end_at"):
            missing_clockout.append(emp["display_name"])
            shifts.append({"employee_id": emp["id"], "name": emp["display_name"],
                           "role": role, "job_title": title, "hours": 0.0,
                           "declared_cents": declared, "missing_clockout": True,
                           "start_at": tc["start_at"]})
            continue
        t_in, t_out = _iso(tc["start_at"]), _iso(tc["end_at"])
        if t_out <= t_in:
            bad_interval.append(
                f"{emp['display_name']} ({t_in.astimezone(tz).strftime('%H:%M')} → "
                f"{t_out.astimezone(tz).strftime('%H:%M')})")
            shifts.append({"employee_id": emp["id"], "name": emp["display_name"],
                           "role": role, "job_title": title, "hours": 0.0,
                           "declared_cents": declared, "invalid_interval": True,
                           "start_at": tc["start_at"], "end_at": tc["end_at"]})
            continue

        seconds = t_out.timestamp() - t_in.timestamp()
        for b in tc.get("breaks", []) or []:
            if b.get("is_paid") or not b.get("end_at"):
                continue
            b0, b1 = _iso(b["start_at"]).timestamp(), _iso(b["end_at"]).timestamp()
            seconds -= max(0.0, min(b1, t_out.timestamp()) - max(b0, t_in.timestamp()))
        # Poquitos: hours as Square reports them, to the hundredth of an hour
        # (owner 2026-08-14). Nearest, NOT the round-up-to-0.05 rule Tavern
        # Law uses — that is what made these figures drift from the venue's
        # previous tip-pool service.
        hours = (Decimal(round(seconds)) / 3600).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        shifts.append({"employee_id": emp["id"], "name": emp["display_name"],
                       "role": role, "job_title": title, "hours": float(hours),
                       "declared_cents": declared,
                       # clock times so period reports can split a shift at
                       # midnight the way Square's labor day does, and run
                       # weekly overtime across day boundaries; the rate is
                       # the one in force for THIS job, so a person who
                       # bartends and hosts is costed correctly for each
                       "start_at": tc["start_at"], "end_at": tc["end_at"],
                       "rate_cents": ((tc.get("wage") or {}).get("hourly_rate")
                                      or {}).get("amount")})

    issues = []
    if unmapped_members:
        issues.append({"severity": "blocking", "code": "unmapped_team_member",
                       "detail": sorted(set(unmapped_members)),
                       "blocks": ["shifts", "cash_tips_cents"]})
    if unmapped_jobs:
        issues.append({"severity": "blocking", "code": "unmapped_job_title",
                       "detail": sorted(set(unmapped_jobs)),
                       "blocks": ["shifts"]})
    if missing_clockout:
        issues.append({"severity": "warning", "code": "missing_clockout",
                       "detail": sorted(missing_clockout)})
    if bad_interval:
        issues.append({"severity": "warning", "code": "invalid_timecard",
                       "detail": sorted(bad_interval)})
    if declared_any and not nonzero_declared:
        issues.append({"severity": "warning", "code": "all_cash_tips_zero",
                       "detail": "every declared cash tip is $0 — possible skipped declarations"})

    return {
        "shifts": sorted(shifts, key=lambda s: (s["name"], s["role"])),
        "cash_tips_cents": cash,
        "issues": issues,
    }
