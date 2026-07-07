/* Tavern Law Tip Pool — vanilla SPA, hash routing.
   Money crosses the API as integer cents; dollars only in the DOM. */

"use strict";

const view = document.getElementById("view");
const topbar = document.getElementById("topbar");
let ME = null;

/* ---------- helpers ---------- */

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  const vid = sessionStorage.getItem("venueId");
  if (vid) headers["X-Venue-Id"] = vid;  // venue scope (M5)
  const res = await fetch(path, {
    headers,
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && !path.endsWith("/login")) {
    ME = null;
    location.hash = "#/login";
    throw new Error("signed out");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, isErr = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = isErr ? "err" : "";
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, isErr ? 5000 : 1800);
}

const fmt = (cents) =>
  (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });

function centsFromInput(el) {
  const v = parseFloat(String(el.value).replace(/[$,]/g, ""));
  return Number.isFinite(v) ? Math.round(v * 100) : 0;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(c));
  }
  return node;
}

const esc = (s) => String(s);

const FLAG_TEXT = {
  negative_foh_pool:
    "Negative FOH pool — kitchen allocation exceeds tips. FOH payouts are $0; owner decides how to cover the shortfall.",
  boh_allocation_without_boh:
    "Food sales but no kitchen staff marked as worked — the BOH share has nowhere to go.",
  undistributed_foh_pool: "Tips entered but no FOH hours — pool is undistributed.",
  undistributed_gratuity: "Auto-gratuity entered but no FOH hours.",
  negative_gratuity: "Auto-gratuity is negative — check the entry.",
};

/* ---------- router ---------- */

const routes = {
  login: renderLogin,
  venues: renderVenuePicker,      // M5: pick a venue before anything else
  day: renderDayDispatch,         // per-venue tip model
  "day-classic": renderDayLegacy, // TL fallback for one pay period, then delete
  period: renderPeriod,
  export: renderExport,
  employees: renderEmployees,
  settings: renderSettings,
  users: renderUsers,
  audit: renderAudit,
};

function renderDayDispatch(dateArg) {
  return ME.venue.tip_model === "PERCENT_TIPOUT"
    ? renderDayLF(dateArg)
    : renderDay(dateArg);
}

const ISSUE_TEXT = {
  unmapped_category: (d) =>
    `Unmapped Square categories: ${Object.values(d).join(", ")}. Map them in Setup, then pull again — food sales are blocked until then.`,
  unmapped_team_member: (d) =>
    `Unknown Square team members clocked in (${d.join(", ")}). Link them in Setup, then pull again — hours and cash tips are blocked until then.`,
  missing_clockout: (d) =>
    `Missing clock-out: ${d.join(", ")} — their FOH hours were skipped; adjust manually if needed.`,
  all_cash_tips_zero: () =>
    "Every declared cash tip is $0 — possible skipped declarations at clock-out.",
  uncataloged_line_items: (d) =>
    `${fmt(d.gross_cents)} of custom-amount sales have no catalog item and were not counted as food.`,
  unattributed_tips: (d) =>
    `${fmt(d.cents)} in card tips have no server attached — assign them or mark house before finalizing.`,
  role_mismatch: (d) => `Role mismatch (assigned role wins): ${d.join("; ")}`,
};

const LF_INFO_FLAGS = new Set(["no_host_resplit"]);
const LF_FLAG_TEXT = {
  no_host_resplit:
    "Reminder: no host tonight — the 10% host share went to the busser pool. Servers 65% / bussers 30% / kitchen 5%.",
  no_host_low_bussers:
    "No host AND thin busser coverage tonight — check staffing (threshold set in Setup).",
  busser_pool_returned_to_servers:
    "No bussers tonight — the busser pool returned to the contributing servers.",
  boh_pool_returned_to_servers:
    "No kitchen staff tonight — the kitchen pool returned to the contributing servers.",
  host_pool_returned_to_servers:
    "Host pool had no recipients — returned to the contributing servers.",
  undistributed_gratuity: "Auto-gratuity entered but no front-of-house hours.",
  unattributed_tips_unresolved:
    "Unattributed tips remain — assign them to a server or mark them house before finalizing.",
  unattributed_tips_overresolved:
    "Assignments exceed the unattributed bucket — reduce them before finalizing.",
};

const sortNumKeys = (o) =>
  Object.fromEntries(Object.keys(o).sort((a, b) => a - b).map((k) => [k, o[k]]));
const deepEq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

async function route() {
  let [name, arg] = location.hash.replace(/^#\//, "").split("/");
  if (!ME && name !== "login") {
    try { ME = await api("/api/me"); } catch { return; }
  }
  // M5: venue must be picked before any venue-scoped screen
  if (ME && name !== "login" && name !== "venues"
      && !sessionStorage.getItem("venueId") && (ME.venues || []).length > 1) {
    name = "venues";
  }
  const fn = routes[name] || (() => { location.hash = ME ? "#/day" : "#/login"; });
  topbar.hidden = !ME || name === "login" || name === "venues";
  const chip = document.getElementById("venuechip");
  if (ME) chip.textContent = ME.venue?.name || "venue";
  document.querySelectorAll("#topbar a").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === name);
    if (a.hasAttribute("data-admin")) a.style.display = ME?.role === "admin" ? "" : "none";
    if (a.hasAttribute("data-super")) a.style.display = ME?.super_admin ? "" : "none";
  });
  view.textContent = "";
  view.className = name === "audit" ? "auditpage" : "";
  try { await fn(arg); } catch (e) { toast(e.message, true); }
}

document.getElementById("venuechip").addEventListener("click", () => {
  sessionStorage.removeItem("venueId");
  location.hash = "#/venues";
  route();
});

async function renderVenuePicker() {
  const cards = el("div", { class: "venuecards" },
    el("h1", { style: "text-align:center" }, "Choose a venue"),
    el("div", { class: "hint", style: "text-align:center;margin-bottom:8px" },
      "Everything you see next — days, staff, exports — belongs to the venue you pick."));
  for (const v of ME.venues || []) {
    const card = el("button", { class: "venuecard", type: "button" },
      el("div", { class: "vn" }, v.name),
      el("div", { class: "vm" },
        v.tip_model === "PERCENT_TIPOUT" ? "Percent tip-out (65/20/10/5)" : "Hourly tip pool"));
    card.addEventListener("click", async () => {
      sessionStorage.setItem("venueId", String(v.id));
      ME = await api("/api/me");  // re-read scoped to the chosen venue
      location.hash = "#/day";
      route();
    });
    cards.append(card);
  }
  view.append(cards);
}

window.addEventListener("hashchange", route);
document.getElementById("logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  ME = null;
  location.hash = "#/login";
});

/* ---------- login ---------- */

function renderLogin() {
  const email = el("input", { type: "email", autocomplete: "username", placeholder: "email" });
  const pass = el("input", { type: "password", autocomplete: "current-password", placeholder: "password" });
  const form = el("form", { class: "login" },
    el("h1", {}, "Tavern Law"),
    el("div", { class: "sub" }, "Tip Pool"),
    el("label", {}, "Email"), email,
    el("label", {}, "Password"), pass,
    el("div", { style: "margin-top:16px" },
      el("button", { type: "submit", style: "width:100%" }, "Sign in")),
  );
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await api("/api/login", { method: "POST", body: { email: email.value, password: pass.value } });
      ME = await api("/api/me");
      location.hash = "#/day";
    } catch (e) { toast(e.message, true); }
  });
  view.append(form);
  email.focus();
}

/* ---------- daily review ---------- */

const MONEY_FIELDS = [
  ["food_sales_cents", "Food sales (non-alcohol)"],
  ["event_food_sales_cents", "Event food sales"],
  ["credit_tips_cents", "Credit card tips"],
  ["cash_tips_cents", "Cash tips (declared)"],
  ["event_tips_cents", "Event tips"],
  ["auto_gratuity_cents", "Auto-gratuity (service charges)"],
];

/* ---------- daily review: stepper wizard (Direction C) ----------
   Confirm → Enter → Review → Lock. Same API, same collectInputs shape,
   same debounced PUT; steps are local view state only. */

const STEP_LABELS = ["Confirm", "Enter", "Review", "Lock"];
const STEPPER_MONEY = [
  ["food_sales_cents", "Food sales (non-alcohol)"],
  ["credit_tips_cents", "Credit card tips"],
  ["auto_gratuity_cents", "Auto-gratuity"],
  ["cash_tips_cents", "Cash tips (declared)"],
];

async function renderDay(dateArg) {
  const dateStr = dateArg || ME.today;
  const [day, employees] = await Promise.all([
    api(`/api/days/${dateStr}`),
    api("/api/employees"),
  ]);
  const foh = employees.filter((e) => e.pool_role === "FOH" && e.active);
  const boh = employees.filter((e) => e.pool_role === "BOH" && e.active);
  const finalized = day.status === "finalized";
  const inputs = day.inputs;
  const sq = day.square;
  const sqVal = sq?.values || {};

  /* ---- local view state ---- */
  let step = finalized ? 4 : 1;
  let computed = day.computed;
  let cashConfirmed = false;
  const clockResolved = new Set(); // employee ids acknowledged (0h or edited)
  let saveTimer = null, saving = false;

  const missingNames = new Set(
    (sq?.issues || []).find((i) => i.code === "missing_clockout")?.detail || []);
  const cashZeroFlagged = (sq?.issues || []).some((i) => i.code === "all_cash_tips_zero");
  const blockedFields = sq?.blocked_fields || [];

  /* ---- shared save/compute plumbing (same PUT payload as legacy) ---- */
  const moneyEls = {}, hourEls = {}, bohChecks = {};
  function collectInputs() {
    const out = { boh_worked: [], foh_hours: {} };
    for (const key of [...STEPPER_MONEY.map(([k]) => k),
                       "event_food_sales_cents", "event_tips_cents"]) {
      out[key] = centsFromInput(moneyEls[key]);
    }
    boh.forEach((e) => { if (bohChecks[e.id].checked) out.boh_worked.push(e.id); });
    foh.forEach((e) => {
      const h = parseFloat(hourEls[e.id].value);
      if (h > 0) out.foh_hours[e.id] = h;
    });
    return out;
  }
  const statusEl = el("span", { class: "status" },
    finalized ? `Finalized ${day.finalized_at?.slice(0, 10) || ""}` : "");
  function scheduleSave() {
    if (finalized) return;
    statusEl.textContent = "…";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 600);
  }
  async function saveNow() {
    if (finalized) return;
    if (saving) { scheduleSave(); return; }  // don't drop edits made mid-save
    saving = true;
    try {
      const updated = await api(`/api/days/${dateStr}`, { method: "PUT", body: collectInputs() });
      computed = updated.computed;
      statusEl.textContent = "Saved";
      refreshAll();
    } catch (e) {
      statusEl.textContent = "";
      toast(e.message, true);
    } finally {
      saving = false;
    }
  }

  /* ---- provenance (derived live, same rules as legacy refreshBadges) ---- */
  function provenance(field) {
    if (!sq) return "manual";
    if (blockedFields.includes(field)) return "blocked";
    if (!(field in sqVal)) return "manual";
    let cur;
    const c = collectInputs();
    if (field === "foh_hours") cur = sortNumKeys(c.foh_hours);
    else if (field === "boh_worked") cur = c.boh_worked.slice().sort((a, b) => a - b);
    else cur = c[field];
    let ref = sqVal[field];
    if (field === "foh_hours") ref = sortNumKeys(ref);
    if (field === "boh_worked") ref = ref.slice().sort((a, b) => a - b);
    return deepEq(cur, ref) ? "square" : "override";
  }

  /* ---- header ---- */
  const dayDate = new Date(dateStr + "T12:00:00");
  const nice = dayDate.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  const caption = el("div", { class: "hint" }, "");
  const datePick = el("input", { type: "date", value: dateStr,
    style: "position:absolute;opacity:0;width:1px;height:1px;min-height:0;padding:0;border:0" });
  datePick.addEventListener("change", () => { location.hash = `#/day/${datePick.value}`; });
  const shift = (days) => {
    const d = new Date(dateStr + "T12:00:00");
    d.setDate(d.getDate() + days);
    location.hash = `#/day/${d.toISOString().slice(0, 10)}`;
  };
  view.append(
    el("div", { class: "row spread", style: "margin:6px 2px 12px" },
      el("div", { style: "position:relative" },
        el("h1", { style: "margin:0", onclick: () => { try { datePick.showPicker(); } catch { datePick.focus(); } } },
          nice), datePick, caption),
      el("div", { class: "row" },
        el("button", { class: "ghost small", onclick: () => shift(-1) }, "‹"),
        el("button", { class: "ghost small", onclick: () => shift(1) }, "›"),
        el("span", { class: `badge ${day.status}` }, day.status.replace("_", " ")))),
  );

  /* ---- progress rail ---- */
  const rail = el("div", { class: "rail" });
  function renderRail() {
    rail.textContent = "";
    for (let i = 1; i <= 4; i++) {
      if (i > 1) rail.append(el("div", { class: `conn ${step >= i || finalized ? "done" : ""}` }));
      const state = finalized ? "done" : i < step ? "done" : i === step ? "current" : "";
      const dot = el("button", { class: `dot ${state}`, type: "button",
                                 ...(finalized ? { disabled: "" } : {}) },
        el("span", { class: "circle" }, state === "done" ? "✓" : String(i)),
        el("span", { class: "lbl" }, STEP_LABELS[i - 1]));
      if (!finalized) dot.addEventListener("click", () => goTo(i));
      rail.append(dot);
    }
    caption.textContent = finalized ? "Finalized" : `Step ${step} of 4 · ${STEP_LABELS[step - 1]}`;
  }
  view.append(rail);

  /* blocking issues are unmissable on every step */
  for (const issue of (sq?.issues || []).filter((i) => i.severity === "blocking")) {
    view.append(el("div", { class: "flag bad" },
      (ISSUE_TEXT[issue.code] || (() => issue.code))(issue.detail)));
  }

  /* ---- step containers ---- */
  const panes = { 1: el("div"), 2: el("div"), 3: el("div"), 4: el("div") };
  view.append(panes[1], panes[2], panes[3], panes[4]);

  /* ===== STEP 1 — Confirm ===== */
  {
    const p = panes[1];
    p.append(el("h2", { class: "stephead" }, "Confirm what Square pulled"),
             el("p", { class: "stepsub" }, "Edit any value to override — the badge flips and you can revert."));
    if (!finalized) {
      const pullBtn = el("button", { class: "ghost small" }, "⟳ Pull from Square");
      pullBtn.addEventListener("click", async () => {
        pullBtn.disabled = true;
        try {
          await api(`/api/days/${dateStr}/pull`, { method: "POST" });
          toast("Pulled from Square");
          route();
        } catch (e) { toast(e.message, true); pullBtn.disabled = false; }
      });
      p.append(el("div", { class: "pullbar" }, pullBtn,
        el("span", { class: "hint" },
          sq ? `Last pulled ${sq.pulled_at.replace("T", " ").slice(0, 16)} UTC` : "Not pulled yet")));
    }
    for (const issue of (sq?.issues || []).filter(
        (i) => i.severity === "warning" && !["all_cash_tips_zero", "missing_clockout"].includes(i.code))) {
      p.append(el("div", { class: "flag" }, (ISSUE_TEXT[issue.code] || (() => issue.code))(issue.detail)));
    }
    for (const [key, label] of STEPPER_MONEY) {
      const input = el("input", { inputmode: "decimal", type: "text",
        value: (inputs[key] / 100).toFixed(2), ...(finalized ? { disabled: "" } : {}) });
      input.addEventListener("input", scheduleSave);
      input.addEventListener("blur", () => { input.value = (centsFromInput(input) / 100).toFixed(2); });
      moneyEls[key] = input;
      const badge = el("span", { class: "src" }, "");
      const revert = el("span", { class: "revert", hidden: "" }, "↩ revert");
      revert.addEventListener("click", () => {
        if (finalized || !(key in sqVal)) return;
        input.value = (sqVal[key] / 100).toFixed(2);
        scheduleSave(); refreshAll();
      });
      const isCashCard = key === "cash_tips_cents" && cashZeroFlagged;
      const card = el("div", { class: `scard ${isCashCard ? "warncard" : ""}` },
        el("div", { class: "top" }, el("span", { class: "lab" }, label),
          el("div", { class: "row", style: "gap:8px" }, revert, badge)),
        el("div", { class: "amt" }, el("span", { class: "cur" }, "$"), input));
      if (isCashCard) {
        card.append(el("div", { class: "warntext" }, el("span", {}, "⚠"),
          el("span", {}, "Every declared cash tip is $0 — confirm nothing was skipped at clock-out, or type the real amount.")));
      }
      card._badge = badge; card._revert = revert; card._key = key;
      p.append(card);
      (p._cards = p._cards || []).push(card);
    }
  }

  /* ===== STEP 2 — Enter ===== */
  const hourRows = {};
  {
    const p = panes[2];
    p.append(el("h2", { class: "stephead" }, "Hours & manual entries"),
             el("p", { class: "stepsub" }, "Most nights you touch nothing here."));
    p.append(el("div", { class: "seclabel" }, "FOH — tippable hours"));
    const hoursCard = el("div", { class: "card", style: "padding:4px 12px" });
    for (const e of foh) {
      const missing = missingNames.has(e.display_name) && !(e.id in inputs.foh_hours) &&
                      !(String(e.id) in inputs.foh_hours);
      const initial = inputs.foh_hours[e.id] ?? inputs.foh_hours[String(e.id)] ?? 0;
      const hidden = el("input", { type: "hidden", value: String(initial) });
      hourEls[e.id] = hidden;

      const sub = el("div", { class: "sub" }, "");
      const rowBadge = el("span", { class: "src", hidden: "" }, "override");
      const valueBtn = el("button", { class: "hbtn", type: "button",
                                      ...(finalized ? { disabled: "" } : {}) });
      const editWrap = el("div", { class: "hedit", hidden: "" });
      const editInput = el("input", { inputmode: "decimal", type: "text" });
      const doneBtn = el("button", { class: "done", type: "button" }, "Done");
      editWrap.append(el("div", { class: "field" }, editInput,
        el("span", { class: "unit" }, "h")), doneBtn);

      const row = el("div", { class: `hrow ${missing ? "warnrow" : ""}` },
        el("div", { class: "who" },
          el("div", { class: "row", style: "gap:8px" },
            el("span", { class: "nm" }, e.display_name), rowBadge), sub),
        el("div", { style: "flex:none" }, valueBtn, editWrap));
      const skipBtn = missing
        ? el("button", { class: "skipbtn", type: "button" }, "Record 0h — worked but never clocked out")
        : null;

      function openEdit() {
        if (finalized) return;
        editInput.value = String(parseFloat(hidden.value) || 0);
        valueBtn.hidden = true; editWrap.hidden = false;
        editInput.focus(); editInput.select();
      }
      function commitEdit() {
        const v = parseFloat(editInput.value);
        hidden.value = String(Number.isFinite(v) && v >= 0 ? Math.min(v, 24) : 0);
        clockResolved.add(e.id);
        editWrap.hidden = true; valueBtn.hidden = false;
        scheduleSave(); refreshAll();
      }
      valueBtn.addEventListener("click", openEdit);
      doneBtn.addEventListener("click", commitEdit);
      editInput.addEventListener("keydown", (ev) => { if (ev.key === "Enter") commitEdit(); });
      if (skipBtn) skipBtn.addEventListener("click", () => {
        hidden.value = "0";
        clockResolved.add(e.id);
        skipBtn.remove();
        scheduleSave(); refreshAll();
      });

      hourRows[e.id] = { emp: e, hidden, valueBtn, sub, rowBadge, row, missing, skipBtn };
      hoursCard.append(row);
      if (skipBtn) hoursCard.append(skipBtn);
    }
    if (!foh.length) hoursCard.append(el("div", { class: "note" }, "No FOH staff yet — add them on the Staff screen."));
    p.append(hoursCard);

    p.append(el("div", { class: "seclabel" }, "Kitchen — worked tonight"));
    const bohCard = el("div", { class: "card bohgrid", style: "padding:6px 8px" });
    for (const e of boh) {
      const state = { checked: inputs.boh_worked.includes(e.id) };
      bohChecks[e.id] = state;
      const box = el("span", { class: "box" }, state.checked ? "✓" : "");
      const chip = el("button", { class: `chip ${state.checked ? "on" : ""}`, type: "button",
                                  ...(finalized ? { disabled: "" } : {}) },
        box, el("span", {}, e.display_name));
      chip.addEventListener("click", () => {
        state.checked = !state.checked;
        chip.classList.toggle("on", state.checked);
        box.textContent = state.checked ? "✓" : "";
        scheduleSave(); refreshAll();
      });
      bohCard.append(chip);
    }
    if (!boh.length) bohCard.append(el("div", { class: "note" }, "No BOH staff yet."));
    p.append(bohCard);

    p.append(el("div", { class: "seclabel" }, "Event sales & tips · usually $0"));
    const evGrid = el("div", { class: "eventgrid" });
    for (const [key, label] of [["event_food_sales_cents", "Event food"],
                                ["event_tips_cents", "Event tips"]]) {
      const input = el("input", { inputmode: "decimal", type: "text",
        value: (inputs[key] / 100).toFixed(2), ...(finalized ? { disabled: "" } : {}) });
      input.addEventListener("input", scheduleSave);
      input.addEventListener("blur", () => { input.value = (centsFromInput(input) / 100).toFixed(2); });
      moneyEls[key] = input;
      evGrid.append(el("div", { class: "scard" },
        el("div", { class: "lab" }, label),
        el("div", { class: "amt" }, el("span", { class: "cur" }, "$"), input)));
    }
    p.append(evGrid);
  }

  /* ===== STEP 3 — Review ===== */
  {
    const p = panes[3];
    p.append(el("h2", { class: "stephead" }, "Review the distribution"),
             el("p", { class: "stepsub" }, "Scan for anything obviously off before you lock it."));
    p.append(el("div", { class: "flags-slot" }), el("div", { class: "pools" }),
             el("div", { class: "seclabel" }, "FOH payouts"),
             el("div", { class: "ptable foh-slot" }),
             el("div", { class: "seclabel" }, "Kitchen shares"),
             el("div", { class: "ptable boh-slot" }));
  }
  function renderReview() {
    const p = panes[3];
    const flags = p.querySelector(".flags-slot");
    flags.textContent = "";
    for (const [flag, on] of Object.entries(computed.flags)) {
      if (on) flags.append(el("div",
        { class: `flag ${flag === "negative_foh_pool" ? "bad" : ""}` }, FLAG_TEXT[flag] || flag));
    }
    const pools = p.querySelector(".pools");
    pools.textContent = "";
    const t = computed.totals;
    for (const [v, k] of [[t.foh_pool_cents, "FOH pool"], [t.boh_allocation_cents, "Kitchen share"],
                          [t.total_tips_cents, "Total tips"], [t.auto_gratuity_cents, "Auto-gratuity"]]) {
      pools.append(el("div", { class: "pool" },
        el("div", { class: "v" }, fmt(v)), el("div", { class: "k" }, k)));
    }
    const fohT = p.querySelector(".foh-slot");
    fohT.textContent = "";
    fohT.append(el("div", { class: "prow phead" },
      el("span", { class: "cname" }, "Name"), el("span", { class: "chrs" }, "Hrs"),
      el("span", { class: "ctips" }, "Tips"), el("span", { class: "cgrat" }, "Grat")));
    for (const r of computed.foh) {
      fohT.append(el("div", { class: "prow" },
        el("span", { class: "cname" }, esc(r.name)), el("span", { class: "chrs" }, String(r.hours)),
        el("span", { class: "ctips" }, fmt(r.tips_cents)), el("span", { class: "cgrat" }, fmt(r.gratuity_cents))));
    }
    const bohT = p.querySelector(".boh-slot");
    bohT.textContent = "";
    for (const r of computed.boh) {
      bohT.append(el("div", { class: "prow", style: "justify-content:space-between" },
        el("span", { class: "cname", style: "flex:none" }, esc(r.name)),
        el("span", {}, fmt(r.share_cents))));
    }
    if (!computed.boh.length) bohT.append(el("div", { class: "note" }, "No kitchen staff marked tonight."));
  }

  /* ===== STEP 4 — Lock ===== */
  {
    const p = panes[4];
    if (!finalized) {
      p.append(el("h2", { class: "stephead" }, "Lock this day"),
               el("p", { class: "stepsub" }, "Finalizing writes an immutable snapshot. Here's exactly what gets locked."),
               el("div", { class: "hero" },
                 el("div", { class: "k" }, "Payout total"),
                 el("div", { class: "v total-slot" }, ""),
                 el("div", { class: "sub head-slot" }, "")),
               el("div", { class: "seclabel" }, "Locking in"),
               el("div", { class: "locklist" }));
    }
  }
  function payoutTotal() {
    const t = computed.totals;
    return t.foh_pool_cents + t.boh_allocation_cents + t.auto_gratuity_cents;
  }
  function lockItems() {
    const items = [];
    const c = collectInputs();
    for (const [key, label] of STEPPER_MONEY) {
      if (key in sqVal && c[key] !== sqVal[key]) {
        items.push({ icon: "✎", warn: false, t: `${label} overridden`,
                     d: `Square ${fmt(sqVal[key])} → ${fmt(c[key])}` });
      }
    }
    if (sqVal.foh_hours) {
      for (const e of foh) {
        const cur = c.foh_hours[e.id] ?? 0;
        const ref = sqVal.foh_hours[String(e.id)] ?? 0;
        if (cur !== ref && !(missingNames.has(e.display_name) && clockResolved.has(e.id))) {
          items.push({ icon: "✎", warn: false, t: `${e.display_name} hours adjusted`,
                       d: `${ref}h → ${cur}h` });
        }
      }
    }
    if (sqVal.boh_worked && !deepEq(c.boh_worked.slice().sort((a, b) => a - b),
                                    sqVal.boh_worked.slice().sort((a, b) => a - b))) {
      items.push({ icon: "✎", warn: false, t: "Kitchen roster edited",
                   d: `${c.boh_worked.length} marked as worked` });
    }
    if (c.event_food_sales_cents || c.event_tips_cents) {
      items.push({ icon: "＋", warn: false, t: "Event entries",
                   d: `Event food ${fmt(c.event_food_sales_cents)} · event tips ${fmt(c.event_tips_cents)}` });
    }
    if (cashZeroFlagged && cashConfirmed && c.cash_tips_cents === 0) {
      items.push({ icon: "✓", warn: false, t: "Zero cash tips confirmed",
                   d: "All declarations were $0 — acknowledged, not a skip" });
    }
    for (const e of foh) {
      if (missingNames.has(e.display_name) && clockResolved.has(e.id)) {
        const cur = c.foh_hours[e.id] ?? 0;
        items.push({ icon: "✓", warn: false, t: `${e.display_name} clock-out resolved`,
                     d: cur ? `Recorded ${cur}h manually` : "Recorded 0h — worked but never clocked out" });
      }
    }
    if (!items.length) {
      items.push({ icon: "✓", warn: false, t: "Clean day — straight from Square",
                   d: "No overrides, no adjustments, no open warnings" });
    }
    return items;
  }
  function renderLock() {
    if (finalized) return;
    const p = panes[4];
    p.querySelector(".total-slot").textContent = fmt(payoutTotal());
    const c = collectInputs();
    p.querySelector(".head-slot").textContent =
      `${Object.keys(c.foh_hours).length} FOH · ${c.boh_worked.length} kitchen`;
    const list = p.querySelector(".locklist");
    list.textContent = "";
    for (const item of lockItems()) {
      list.append(el("div", { class: "lockitem" },
        el("span", { class: `ic ${item.warn ? "warnic" : ""}` }, item.icon),
        el("div", { style: "flex:1" },
          el("div", { class: "t" }, item.t), el("div", { class: "d" }, item.d))));
    }
  }

  /* ===== finalized summary (replaces step 4 content) ===== */
  if (finalized) {
    const t = computed.totals;
    const version = day.snapshots.length ? day.snapshots[day.snapshots.length - 1].version : 1;
    panes[4].append(el("div", { style: "text-align:center;padding:26px 6px 10px" },
      el("div", { class: "donecircle" }, "✓"),
      el("h2", { class: "stephead", style: "text-align:center" }, "Day finalized"),
      el("p", { class: "stepsub", style: "text-align:center;max-width:280px;margin:0 auto 20px" },
        `Snapshot v${version} saved · ${nice} · ${fmt(t.foh_pool_cents + t.boh_allocation_cents + t.auto_gratuity_cents)} locked. Reopen requires an admin.`),
      el("div", { class: "finsummary" },
        el("div", { class: "r" }, el("span", {}, "FOH pool"), el("span", {}, fmt(t.foh_pool_cents))),
        el("div", { class: "r" }, el("span", {}, "Kitchen share"), el("span", {}, fmt(t.boh_allocation_cents))),
        el("div", { class: "r" }, el("span", {}, "Auto-gratuity"), el("span", {}, fmt(t.auto_gratuity_cents))))));
  }

  /* ---- step gating + footer ---- */
  function unresolvedClockouts() {
    return foh.filter((e) => missingNames.has(e.display_name) && !clockResolved.has(e.id));
  }
  function cashGateOpen() {
    return cashZeroFlagged && !cashConfirmed && centsFromInput(moneyEls.cash_tips_cents) === 0;
  }

  const backBtn = el("button", { class: "ghost", type: "button" }, "Back");
  const primaryBtn = el("button", { class: "primary-grow", type: "button" }, "");
  backBtn.addEventListener("click", () => goTo(step - 1));
  primaryBtn.addEventListener("click", onPrimary);

  function refreshFooter() {
    if (finalized) {
      backBtn.hidden = true;
      if (ME.role === "admin") {
        primaryBtn.className = "danger primary-grow";
        primaryBtn.textContent = "Reopen day";
        primaryBtn.disabled = false;
      } else {
        primaryBtn.className = "ghost primary-grow";
        primaryBtn.textContent = "View period";
        primaryBtn.disabled = false;
      }
      return;
    }
    backBtn.hidden = step === 1;
    primaryBtn.disabled = false;
    primaryBtn.className = "primary-grow";
    if (step === 1) {
      primaryBtn.textContent = cashGateOpen() ? "Confirm $0 cash & continue ›" : "Confirm & continue ›";
      if (cashGateOpen()) primaryBtn.className = "warnbtn primary-grow";
    } else if (step === 2) {
      const open = unresolvedClockouts();
      if (open.length) {
        primaryBtn.textContent = "Resolve clock-out to continue";
        primaryBtn.disabled = true;
        primaryBtn.className = "ghost primary-grow";
      } else {
        primaryBtn.textContent = "Review distribution ›";
      }
    } else if (step === 3) {
      primaryBtn.textContent = "Go to finalize ›";
    } else {
      if (blockedFields.length) {
        primaryBtn.textContent = "Blocked — fix mappings in Setup";
        primaryBtn.disabled = true;
        primaryBtn.className = "ghost primary-grow";
      } else {
        primaryBtn.textContent = `Finalize — lock ${fmt(payoutTotal())}`;
      }
    }
  }

  async function onPrimary() {
    if (finalized) {
      if (ME.role === "admin") {
        if (!confirm("Reopen this finalized day? A new snapshot version will be written when it is finalized again.")) return;
        await api(`/api/days/${dateStr}/reopen`, { method: "POST" });
        route();
      } else {
        location.hash = `#/period/${dateStr}`;
      }
      return;
    }
    if (step === 1 && cashGateOpen()) {
      cashConfirmed = true;
      goTo(2);
      return;
    }
    if (step < 4) { goTo(step + 1); return; }
    clearTimeout(saveTimer);
    await saveNow();
    primaryBtn.disabled = true;
    try {
      await api(`/api/days/${dateStr}/finalize`, { method: "POST" });
      toast("Day finalized");
      route();
    } catch (e) {
      toast(e.message, true);
      primaryBtn.disabled = false;
    }
  }

  function goTo(n) {
    if (finalized || n < 1 || n > 4) return;
    if (n > 2 && unresolvedClockouts().length) {
      step = 2;
      toast("Resolve the missing clock-out first", true);
    } else {
      step = n;
    }
    refreshAll();
  }

  /* ---- global refresh: badges, panes, rail, footer, cash badge ---- */
  function refreshAll() {
    renderRail();
    for (const [n, pane] of Object.entries(panes)) {
      pane.style.display = Number(n) === step ? "" : "none";
    }
    for (const card of panes[1]._cards || []) {
      const key = card._key;
      let prov = provenance(key);
      let text = prov;
      if (key === "cash_tips_cents" && cashZeroFlagged && cashConfirmed &&
          centsFromInput(moneyEls[key]) === 0 && prov !== "override") {
        text = "confirmed $0"; prov = "square";
      }
      card._badge.className = `src ${prov}`;
      card._badge.textContent = key in sqVal || prov !== "manual" ? text : "manual";
      card._revert.hidden = prov !== "override" || finalized;
    }
    for (const { emp, hidden, valueBtn, sub, rowBadge, missing } of Object.values(hourRows)) {
      const cur = parseFloat(hidden.value) || 0;
      valueBtn.textContent = "";
      // 2-decimal display, trailing zeros trimmed (6.85, 5.5, 7)
      valueBtn.append(el("span", {}, String(parseFloat(cur.toFixed(2)))),
                      el("span", { class: "unit" }, "h"), el("span", { class: "pen" }, "✎"));
      const ref = sqVal.foh_hours ? (sqVal.foh_hours[String(emp.id)] ?? 0) : null;
      const overridden = ref !== null && cur !== ref;
      rowBadge.hidden = !overridden;
      if (missing && !clockResolved.has(emp.id)) {
        sub.textContent = "Missing clock-out — enter hours or record 0h";
      } else if (overridden) {
        sub.textContent = "manual override · audit-logged";
      } else {
        sub.textContent = sq ? "from Square timecards" : "manual entry";
      }
    }
    if (step === 3) renderReview();
    renderLock();
    refreshFooter();
  }

  view.append(el("a", { class: "viewswitch", href: `#/day-classic/${dateStr}` }, "Use classic view"));
  view.append(el("div", { class: "actionbar" },
    el("div", {}, statusEl), backBtn, primaryBtn));

  refreshAll();
  if (finalized) renderRail();
}

/* ---------- daily review: PERCENT_TIPOUT stepper (La Fontana, M5) ----------
   Confirm tips → Staff & hours → Review pools → Lock. Same rail/footer
   pattern as the TL stepper; LF inputs shape (server tips, unattributed
   bucket, worked hours). */

const LF_STEPS = ["Confirm", "Enter", "Review", "Lock"];
const LF_ROLE_LABEL = { SERVER: "Servers", BUSSER: "Bussers", HOST: "Hosts", BOH: "Kitchen" };

async function renderDayLF(dateArg) {
  const dateStr = dateArg || ME.today;
  const [day, employees] = await Promise.all([
    api(`/api/days/${dateStr}`),
    api("/api/employees"),
  ]);
  const staff = employees.filter((e) => e.pool_role !== "EXCLUDED" && e.active);
  const servers = staff.filter((e) => e.pool_role === "SERVER");
  const finalized = day.status === "finalized";
  const inputs = day.inputs;
  const sq = day.square;
  const sqVal = sq?.values || {};

  let step = finalized ? 4 : 1;
  let computed = day.computed;
  let saveTimer = null, saving = false;

  /* ---- input state (plain objects; collectInputs reads .value) ---- */
  const tipEls = {}, cashEls = {}, hourEls = {}, assignEls = {};
  const gratEl = { value: (inputs.auto_gratuity_cents / 100).toFixed(2) };
  const houseEl = { value: (inputs.unattributed_house_cents / 100).toFixed(2) };

  function collectInputs() {
    const out = {
      server_tips: { ...(inputs.server_tips || {}) },
      server_cash_tips: { ...(inputs.server_cash_tips || {}) },
      hours: { ...(inputs.hours || {}) },
      unattributed_assignments: { ...(inputs.unattributed_assignments || {}) },
      auto_gratuity_cents: centsFromInput(gratEl),
      unattributed_tips_cents: inputs.unattributed_tips_cents,
      unattributed_house_cents: centsFromInput(houseEl),
    };
    for (const [id, elm] of Object.entries(tipEls)) {
      const c = centsFromInput(elm);
      if (c) out.server_tips[id] = c;
      else delete out.server_tips[id];
    }
    for (const [id, elm] of Object.entries(cashEls)) {
      const c = centsFromInput(elm);
      if (c) out.server_cash_tips[id] = c;
      else delete out.server_cash_tips[id];
    }
    for (const [id, elm] of Object.entries(hourEls)) {
      const h = parseFloat(elm.value);
      if (h > 0) out.hours[id] = h;
      else delete out.hours[id];
    }
    for (const [id, elm] of Object.entries(assignEls)) {
      const c = centsFromInput(elm);
      if (c) out.unattributed_assignments[id] = c;
      else delete out.unattributed_assignments[id];
    }
    return out;
  }

  const statusEl = el("span", { class: "status" },
    finalized ? `Finalized ${day.finalized_at?.slice(0, 10) || ""}` : "");
  function scheduleSave() {
    if (finalized) return;
    statusEl.textContent = "…";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 600);
  }
  async function saveNow() {
    if (finalized) return;
    if (saving) { scheduleSave(); return; }
    saving = true;
    try {
      const updated = await api(`/api/days/${dateStr}`, { method: "PUT", body: collectInputs() });
      computed = updated.computed;
      statusEl.textContent = "Saved";
      refreshAll();
    } catch (e) {
      statusEl.textContent = "";
      toast(e.message, true);
    } finally {
      saving = false;
    }
  }

  /* ---- header + rail (same pattern as the TL stepper) ---- */
  const dayDate = new Date(dateStr + "T12:00:00");
  const nice = dayDate.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  const caption = el("div", { class: "hint" }, "");
  const shift = (days) => {
    const d = new Date(dateStr + "T12:00:00");
    d.setDate(d.getDate() + days);
    location.hash = `#/day/${d.toISOString().slice(0, 10)}`;
  };
  view.append(el("div", { class: "row spread", style: "margin:6px 2px 12px" },
    el("div", {}, el("h1", { style: "margin:0" }, nice), caption),
    el("div", { class: "row" },
      el("button", { class: "ghost small", onclick: () => shift(-1) }, "‹"),
      el("button", { class: "ghost small", onclick: () => shift(1) }, "›"),
      el("span", { class: `badge ${day.status}` }, day.status.replace("_", " ")))));

  const rail = el("div", { class: "rail" });
  function renderRail() {
    rail.textContent = "";
    for (let i = 1; i <= 4; i++) {
      if (i > 1) rail.append(el("div", { class: `conn ${step >= i || finalized ? "done" : ""}` }));
      const state = finalized ? "done" : i < step ? "done" : i === step ? "current" : "";
      const dot = el("button", { class: `dot ${state}`, type: "button",
                                 ...(finalized ? { disabled: "" } : {}) },
        el("span", { class: "circle" }, state === "done" ? "✓" : String(i)),
        el("span", { class: "lbl" }, LF_STEPS[i - 1]));
      if (!finalized) dot.addEventListener("click", () => { step = i; refreshAll(); });
      rail.append(dot);
    }
    caption.textContent = finalized ? "Finalized" : `Step ${step} of 4 · ${LF_STEPS[step - 1]}`;
  }
  view.append(rail);

  for (const issue of (sq?.issues || []).filter((i) => i.severity === "blocking")) {
    view.append(el("div", { class: "flag bad" },
      (ISSUE_TEXT[issue.code] || (() => issue.code))(issue.detail)));
  }

  const panes = { 1: el("div"), 2: el("div"), 3: el("div"), 4: el("div") };
  view.append(panes[1], panes[2], panes[3], panes[4]);

  function moneyInput(store, key, initialCents) {
    const input = el("input", { inputmode: "decimal", type: "text",
      value: (initialCents / 100).toFixed(2), ...(finalized ? { disabled: "" } : {}) });
    input.addEventListener("input", scheduleSave);
    input.addEventListener("blur", () => { input.value = (centsFromInput(input) / 100).toFixed(2); });
    store[key] = input;
    return input;
  }

  /* ===== STEP 1 — Confirm tips ===== */
  {
    const p = panes[1];
    p.append(el("h2", { class: "stephead" }, "Confirm each server's tips"),
             el("p", { class: "stepsub" },
               "Card tips pulled per server from Square. Declared cash fills in once the declaration policy starts."));
    if (!finalized) {
      const pullBtn = el("button", { class: "ghost small" }, "⟳ Pull from Square");
      pullBtn.addEventListener("click", async () => {
        pullBtn.disabled = true;
        try {
          await api(`/api/days/${dateStr}/pull`, { method: "POST" });
          toast("Pulled from Square");
          route();
        } catch (e) { toast(e.message, true); pullBtn.disabled = false; }
      });
      p.append(el("div", { class: "pullbar" }, pullBtn,
        el("span", { class: "hint" },
          sq ? `Last pulled ${sq.pulled_at.replace("T", " ").slice(0, 16)} UTC` : "Not pulled yet")));
    }
    for (const issue of (sq?.issues || []).filter(
        (i) => i.severity === "warning" && i.code !== "unattributed_tips")) {
      p.append(el("div", { class: "flag" }, (ISSUE_TEXT[issue.code] || (() => issue.code))(issue.detail)));
    }
    const srvCard = el("div", { class: "card", style: "padding:4px 12px" });
    if (!servers.length) srvCard.append(el("div", { class: "note" }, "No servers yet — add them on the Staff screen."));
    for (const e of servers) {
      const tip = moneyInput(tipEls, e.id, Number(inputs.server_tips[e.id] ?? inputs.server_tips[String(e.id)] ?? 0));
      const cash = moneyInput(cashEls, e.id, Number(inputs.server_cash_tips[e.id] ?? inputs.server_cash_tips[String(e.id)] ?? 0));
      srvCard.append(el("div", { class: "hrow" },
        el("div", { class: "who" }, el("div", { class: "nm" }, e.display_name),
          el("div", { class: "sub" }, "card tips · declared cash")),
        el("div", { class: "row", style: "gap:6px" },
          el("div", { class: "money", style: "width:104px" }, tip),
          el("div", { class: "money", style: "width:88px" }, cash))));
    }
    p.append(el("div", { class: "seclabel" }, "Servers"), srvCard);

    /* unattributed bucket */
    const bucket = inputs.unattributed_tips_cents;
    if (bucket > 0) {
      const uCard = el("div", { class: "scard warncard" },
        el("div", { class: "top" },
          el("span", { class: "lab" }, "Unattributed tips (no server on the payment)"),
          el("span", { class: "src blocked" }, fmt(bucket))),
        el("div", { class: "warntext" }, el("span", {}, "⚠"),
          el("span", {}, "Assign to a server or mark house — finalize is blocked until every cent is resolved. Never auto-assigned.")));
      for (const e of servers) {
        const inputEl = moneyInput(assignEls, e.id,
          Number(inputs.unattributed_assignments[e.id] ?? inputs.unattributed_assignments[String(e.id)] ?? 0));
        uCard.append(el("div", { class: "hrow" },
          el("div", { class: "who" }, el("div", { class: "nm" }, `→ ${e.display_name}`)),
          el("div", { class: "money", style: "width:104px" }, inputEl)));
      }
      uCard.append(el("div", { class: "hrow" },
        el("div", { class: "who" }, el("div", { class: "nm" }, "→ House (no tip pool)"),
          el("div", { class: "sub" }, "counter sales / house accounts")),
        el("div", { class: "money", style: "width:104px" },
          (() => { const i = moneyInput({}, "x", inputs.unattributed_house_cents);
                   houseEl.value = i.value;
                   i.addEventListener("input", () => { houseEl.value = i.value; });
                   return i; })())));
      const remaining = el("div", { class: "warntext unresolved-slot" });
      uCard.append(remaining);
      p.append(el("div", { class: "seclabel" }, "Needs a decision"), uCard);
      p._unresolvedSlot = remaining;
    }

    const gratCard = el("div", { class: "scard" },
      el("div", { class: "top" }, el("span", { class: "lab" }, "Auto-gratuity (service charges)"),
        el("span", { class: "src grat-badge" }, "")),
      el("div", { class: "amt" }, el("span", { class: "cur" }, "$"),
        (() => { const i = moneyInput({}, "g", inputs.auto_gratuity_cents);
                 gratEl.value = i.value;
                 i.addEventListener("input", () => { gratEl.value = i.value; });
                 return i; })()));
    p.append(el("div", { class: "seclabel" }, "Wages line (separate from tips)"), gratCard);
    p._gratBadge = gratCard.querySelector(".grat-badge");
  }

  /* ===== STEP 2 — Who worked (owner ruling 2026-07-06: presence, not
     hours — single shift, everything splits evenly per role). Pulled Square
     hours are kept under the hood; a manual check stores a 1h marker. ===== */
  {
    const p = panes[2];
    p.append(el("h2", { class: "stephead" }, "Who worked tonight"),
             el("p", { class: "stepsub" },
               "Single shift — pools and gratuity split evenly among each role's workers. Just tap everyone who worked."));
    // Kitchen is NOT tracked daily (ruling 2026-07-06): its 5% accumulates
    // and is split on the monthly export screen.
    for (const role of ["SERVER", "BUSSER", "HOST"]) {
      const group = staff.filter((e) => e.pool_role === role);
      if (!group.length) continue;
      const card = el("div", { class: "card bohgrid", style: "padding:6px 8px" });
      for (const e of group) {
        const pulled = Number(inputs.hours[e.id] ?? inputs.hours[String(e.id)] ?? 0);
        const state = { checked: pulled > 0, hours: pulled > 0 ? pulled : 1 };
        // collectInputs reads .value: worked -> stored hours (pulled value
        // when Square provided one, else a 1h "worked" marker)
        hourEls[e.id] = { get value() { return state.checked ? String(state.hours) : "0"; } };
        const box = el("span", { class: "box" }, state.checked ? "✓" : "");
        const chip = el("button", { class: `chip ${state.checked ? "on" : ""}`, type: "button",
                                    ...(finalized ? { disabled: "" } : {}) },
          box, el("span", {}, e.display_name));
        chip.addEventListener("click", () => {
          state.checked = !state.checked;
          chip.classList.toggle("on", state.checked);
          box.textContent = state.checked ? "✓" : "";
          scheduleSave(); refreshAll();
        });
        card.append(chip);
      }
      p.append(el("div", { class: "seclabel" }, LF_ROLE_LABEL[role]), card);
    }
    p.append(el("div", { class: "note" },
      "Kitchen isn't tracked daily — its 5% accumulates and is split among the month's kitchen roster on the monthly payroll export."));
  }

  /* ===== STEP 3 — Review pools ===== */
  {
    const p = panes[3];
    p.append(el("h2", { class: "stephead" }, "Review the distribution"),
             el("p", { class: "stepsub" }, "Every server's tips split per policy; pools split among who worked."),
             el("div", { class: "flags-slot" }), el("div", { class: "pools" }),
             el("div", { class: "seclabel" }, "Per person"),
             el("div", { class: "ptable people-slot" }));
  }
  function renderReview() {
    const p = panes[3];
    const flags = p.querySelector(".flags-slot");
    flags.textContent = "";
    for (const [flag, on] of Object.entries(computed.flags)) {
      if (!on) continue;
      if (LF_INFO_FLAGS.has(flag)) {
        flags.append(el("div", { class: "note" }, LF_FLAG_TEXT[flag]));
        continue;
      }
      const bad = flag.startsWith("unattributed");
      flags.append(el("div", { class: `flag ${bad ? "bad" : ""}` }, LF_FLAG_TEXT[flag] || flag));
    }
    const pools = p.querySelector(".pools");
    pools.textContent = "";
    const t = computed.totals;
    for (const [v, k] of [[t.total_tips_cents, "Total tips"],
                          [t.pool_busser_cents, "Busser pool"],
                          [t.pool_host_cents, "Host pool"],
                          [t.pool_boh_cents, "Kitchen (paid monthly)"],
                          [t.auto_gratuity_cents, "Auto-gratuity"]]) {
      pools.append(el("div", { class: "pool" },
        el("div", { class: "v" }, fmt(v)), el("div", { class: "k" }, k)));
    }
    const tbl = p.querySelector(".people-slot");
    tbl.textContent = "";
    tbl.append(el("div", { class: "prow phead" },
      el("span", { class: "cname" }, "Name"), el("span", { class: "chrs" }, "Keep"),
      el("span", { class: "ctips" }, "Pool/Ret"), el("span", { class: "cgrat" }, "Total")));
    for (const r of computed.people) {
      tbl.append(el("div", { class: "prow" },
        el("span", { class: "cname" }, `${esc(r.name)} · ${r.role.toLowerCase()}`),
        el("span", { class: "chrs" }, r.keep_cents ? fmt(r.keep_cents) : "—"),
        el("span", { class: "ctips" }, fmt(r.pool_share_cents + r.returned_cents)),
        el("span", { class: "cgrat" }, fmt(r.payout_cents))));
    }
  }

  /* ===== STEP 4 — Lock ===== */
  {
    const p = panes[4];
    if (!finalized) {
      p.append(el("h2", { class: "stephead" }, "Lock this day"),
               el("p", { class: "stepsub" }, "Finalizing writes an immutable snapshot."),
               el("div", { class: "hero" },
                 el("div", { class: "k" }, "Payout total (tips + gratuity)"),
                 el("div", { class: "v total-slot" }, ""),
                 el("div", { class: "sub head-slot" }, "")),
               el("div", { class: "seclabel" }, "Locking in"),
               el("div", { class: "locklist" }));
    } else {
      const t = computed.totals;
      const version = day.snapshots.length ? day.snapshots[day.snapshots.length - 1].version : 1;
      p.append(el("div", { style: "text-align:center;padding:26px 6px 10px" },
        el("div", { class: "donecircle" }, "✓"),
        el("h2", { class: "stephead", style: "text-align:center" }, "Day finalized"),
        el("p", { class: "stepsub", style: "text-align:center;max-width:280px;margin:0 auto 20px" },
          `Snapshot v${version} saved · ${nice} · ${fmt(t.total_tips_cents + t.auto_gratuity_cents)} locked.`),
        el("div", { class: "finsummary" },
          el("div", { class: "r" }, el("span", {}, "Total tips"), el("span", {}, fmt(t.total_tips_cents))),
          el("div", { class: "r" }, el("span", {}, "Busser / Host / Kitchen pools"),
            el("span", {}, `${fmt(t.pool_busser_cents)} / ${fmt(t.pool_host_cents)} / ${fmt(t.pool_boh_cents)}`)),
          el("div", { class: "r" }, el("span", {}, "Auto-gratuity"), el("span", {}, fmt(t.auto_gratuity_cents))))));
    }
  }
  function renderLock() {
    if (finalized) return;
    const p = panes[4];
    const t = computed.totals;
    p.querySelector(".total-slot").textContent = fmt(t.total_tips_cents + t.auto_gratuity_cents);
    const c = collectInputs();
    p.querySelector(".head-slot").textContent =
      `${Object.keys(c.server_tips).length} servers with tips · ${Object.keys(c.hours).length} worked`;
    const list = p.querySelector(".locklist");
    list.textContent = "";
    const items = [];
    for (const [flag, on] of Object.entries(computed.flags)) {
      if (on && LF_FLAG_TEXT[flag] && !LF_INFO_FLAGS.has(flag)) {
        items.push({ icon: "⚑", t: LF_FLAG_TEXT[flag] });
      }
    }
    if (c.unattributed_house_cents > 0) {
      items.push({ icon: "✓", t: `${fmt(c.unattributed_house_cents)} unattributed marked house` });
    }
    const assigned = Object.values(c.unattributed_assignments).reduce((a, b) => a + b, 0);
    if (assigned > 0) items.push({ icon: "✓", t: `${fmt(assigned)} unattributed assigned to servers` });
    if (!items.length) items.push({ icon: "✓", t: "Clean day — straight from Square" });
    for (const item of items) {
      list.append(el("div", { class: "lockitem" },
        el("span", { class: "ic" }, item.icon),
        el("div", { style: "flex:1" }, el("div", { class: "t" }, item.t))));
    }
  }

  /* ---- footer ---- */
  const backBtn = el("button", { class: "ghost", type: "button" }, "Back");
  const primaryBtn = el("button", { class: "primary-grow", type: "button" }, "");
  backBtn.addEventListener("click", () => { step -= 1; refreshAll(); });
  primaryBtn.addEventListener("click", onPrimary);

  function unresolvedCents() {
    const c = collectInputs();
    const assigned = Object.values(c.unattributed_assignments).reduce((a, b) => a + b, 0);
    return c.unattributed_tips_cents - assigned - c.unattributed_house_cents;
  }
  function refreshFooter() {
    if (finalized) {
      backBtn.hidden = true;
      primaryBtn.className = ME.role === "admin" ? "danger primary-grow" : "ghost primary-grow";
      primaryBtn.textContent = ME.role === "admin" ? "Reopen day" : "View period";
      primaryBtn.disabled = false;
      return;
    }
    backBtn.hidden = step === 1;
    primaryBtn.disabled = false;
    primaryBtn.className = "primary-grow";
    const un = unresolvedCents();
    if (step === 1 && un !== 0 && inputs.unattributed_tips_cents > 0) {
      primaryBtn.textContent = un > 0
        ? `Resolve ${fmt(un)} unattributed to continue`
        : "Assignments exceed the bucket";
      primaryBtn.disabled = true;
      primaryBtn.className = "ghost primary-grow";
    } else if (step === 1) {
      primaryBtn.textContent = "Confirm & continue ›";
    } else if (step === 2) {
      primaryBtn.textContent = "Review distribution ›";
    } else if (step === 3) {
      primaryBtn.textContent = "Go to finalize ›";
    } else {
      const t = computed.totals;
      primaryBtn.textContent = `Finalize — lock ${fmt(t.total_tips_cents + t.auto_gratuity_cents)}`;
      if (un !== 0 && inputs.unattributed_tips_cents > 0) {
        primaryBtn.disabled = true;
        primaryBtn.className = "ghost primary-grow";
        primaryBtn.textContent = "Unattributed tips unresolved";
      }
    }
  }
  async function onPrimary() {
    if (finalized) {
      if (ME.role === "admin") {
        if (!confirm("Reopen this finalized day?")) return;
        await api(`/api/days/${dateStr}/reopen`, { method: "POST" });
        route();
      } else {
        location.hash = `#/period/${dateStr}`;
      }
      return;
    }
    if (step < 4) { step += 1; refreshAll(); return; }
    clearTimeout(saveTimer);
    await saveNow();
    primaryBtn.disabled = true;
    try {
      await api(`/api/days/${dateStr}/finalize`, { method: "POST" });
      toast("Day finalized");
      route();
    } catch (e) {
      toast(e.message, true);
      primaryBtn.disabled = false;
    }
  }

  function refreshAll() {
    renderRail();
    for (const [n, pane] of Object.entries(panes)) {
      pane.style.display = Number(n) === step ? "" : "none";
    }
    if (panes[1]._gratBadge && sq) {
      const prov = !("auto_gratuity_cents" in sqVal) ? "manual"
        : centsFromInput(gratEl) === sqVal.auto_gratuity_cents ? "square" : "override";
      panes[1]._gratBadge.className = `src ${prov} grat-badge`;
      panes[1]._gratBadge.textContent = prov;
    }
    if (panes[1]._unresolvedSlot) {
      const un = unresolvedCents();
      panes[1]._unresolvedSlot.textContent = un > 0
        ? `⚠ ${fmt(un)} still unresolved`
        : un < 0 ? "⚠ assignments exceed the bucket" : "✓ fully resolved";
    }
    if (step === 3) renderReview();
    renderLock();
    refreshFooter();
  }

  view.append(el("div", { class: "actionbar" },
    el("div", {}, statusEl), backBtn, primaryBtn));
  refreshAll();
}

async function renderDayLegacy(dateArg) {
  const dateStr = dateArg || ME.today;
  const [day, employees] = await Promise.all([
    api(`/api/days/${dateStr}`),
    api("/api/employees"),
  ]);
  const foh = employees.filter((e) => e.pool_role === "FOH" && e.active);
  const boh = employees.filter((e) => e.pool_role === "BOH" && e.active);
  const finalized = day.status === "finalized";
  const inputs = day.inputs;

  /* --- date navigation --- */
  const dateInput = el("input", { type: "date", value: dateStr });
  dateInput.addEventListener("change", () => { location.hash = `#/day-classic/${dateInput.value}`; });
  const shift = (days) => {
    const d = new Date(dateStr + "T12:00:00");
    d.setDate(d.getDate() + days);
    location.hash = `#/day-classic/${d.toISOString().slice(0, 10)}`;
  };
  view.append(
    el("div", { class: "row spread" },
      el("h1", {}, "Daily Review",
        el("a", { class: "viewswitch", style: "display:inline;margin-left:10px;font-weight:400",
                  href: `#/day/${dateStr}` }, "Try new view")),
      el("span", { class: `badge ${day.status}` }, day.status.replace("_", " "))),
    el("div", { class: "datebar" },
      el("button", { class: "ghost small", onclick: () => shift(-1) }, "‹"),
      dateInput,
      el("button", { class: "ghost small", onclick: () => shift(1) }, "›")),
  );

  /* --- Square pull bar + issues (M3) --- */
  const sq = day.square;
  if (!finalized) {
    const pullBtn = el("button", { class: "ghost small" }, "⟳ Pull from Square");
    pullBtn.addEventListener("click", async () => {
      pullBtn.disabled = true;
      try {
        await api(`/api/days/${dateStr}/pull`, { method: "POST" });
        toast("Pulled from Square");
        route();
      } catch (e) {
        toast(e.message, true);
        pullBtn.disabled = false;
      }
    });
    view.append(el("div", { class: "pullbar" }, pullBtn,
      el("span", { class: "hint" },
        sq ? `Last pulled ${sq.pulled_at.replace("T", " ").slice(0, 16)} UTC` : "Not pulled yet")));
  }
  for (const issue of sq?.issues || []) {
    const text = (ISSUE_TEXT[issue.code] || ((d) => issue.code))(issue.detail);
    view.append(el("div", { class: `flag ${issue.severity === "blocking" ? "bad" : ""}` }, text));
  }

  /* provenance badges: computed live from current inputs vs pulled values */
  const badgeEls = {};
  function makeBadge(field) {
    const b = el("span", { class: "src" }, "");
    badgeEls[field] = b;
    return b;
  }
  function currentFieldValue(field) {
    const cur = collectInputs();
    if (field === "foh_hours") return sortNumKeys(cur.foh_hours);
    if (field === "boh_worked") return cur.boh_worked.slice().sort((a, b) => a - b);
    return cur[field];
  }
  function squareFieldValue(field) {
    const v = sq.values[field];
    if (field === "foh_hours") return sortNumKeys(v);
    if (field === "boh_worked") return v.slice().sort((a, b) => a - b);
    return v;
  }
  function refreshBadges() {
    if (!sq) return;
    for (const [field, b] of Object.entries(badgeEls)) {
      b.onclick = null;
      if (sq.blocked_fields.includes(field)) {
        b.className = "src blocked"; b.textContent = "blocked";
      } else if (!(field in sq.values)) {
        b.className = "src manual"; b.textContent = "manual";
      } else if (deepEq(currentFieldValue(field), squareFieldValue(field))) {
        b.className = "src square"; b.textContent = "Square";
      } else {
        b.className = "src override";
        b.textContent = field.endsWith("_cents")
          ? `override · Square ${fmt(sq.values[field])} — tap to revert`
          : "override — tap to revert";
        b.onclick = () => revertToSquare(field);
      }
    }
  }
  function revertToSquare(field) {
    if (finalized) return;
    const v = sq.values[field];
    if (field === "boh_worked") {
      for (const [eid, cb] of Object.entries(bohChecks)) cb.checked = v.includes(Number(eid));
    } else if (field === "foh_hours") {
      for (const [eid, input] of Object.entries(hourEls)) input.value = v[eid] ?? 0;
    } else {
      moneyEls[field].value = (v / 100).toFixed(2);
    }
    refreshBadges();
    scheduleSave();
  }

  /* --- state + save/compute plumbing --- */
  let saveTimer = null;
  let saving = false;
  const statusEl = el("span", { class: "status" }, finalized ? `Finalized ${day.finalized_at?.slice(0, 10) || ""}` : "");

  function collectInputs() {
    const out = { boh_worked: [], foh_hours: {} };
    for (const [key] of MONEY_FIELDS) out[key] = centsFromInput(moneyEls[key]);
    boh.forEach((e) => { if (bohChecks[e.id].checked) out.boh_worked.push(e.id); });
    foh.forEach((e) => {
      const h = parseFloat(hourEls[e.id].value);
      if (h > 0) out.foh_hours[e.id] = h;
    });
    return out;
  }

  function scheduleSave() {
    if (finalized) return;
    statusEl.textContent = "…";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 600);
  }

  async function saveNow() {
    if (finalized) return;
    if (saving) { scheduleSave(); return; }  // don't drop edits made mid-save
    saving = true;
    try {
      const updated = await api(`/api/days/${dateStr}`, { method: "PUT", body: collectInputs() });
      renderComputed(updated.computed);
      statusEl.textContent = "Saved";
    } catch (e) {
      statusEl.textContent = "";
      toast(e.message, true);
    } finally {
      saving = false;
    }
  }

  /* --- money inputs --- */
  const moneyEls = {};
  const moneyCard = el("div", { class: "card" }, el("h2", {}, "Sales & tips"));
  for (const [key, label] of MONEY_FIELDS) {
    const input = el("input", {
      type: "text", inputmode: "decimal",
      value: (inputs[key] / 100).toFixed(2),
      ...(finalized ? { disabled: "" } : {}),
    });
    input.addEventListener("input", scheduleSave);
    input.addEventListener("blur", () => { input.value = (centsFromInput(input) / 100).toFixed(2); });
    moneyEls[key] = input;
    const labelEl = el("label", {}, label);
    if (sq && key !== "event_food_sales_cents" && key !== "event_tips_cents") {
      labelEl.append(makeBadge(key));
    }
    moneyCard.append(labelEl, el("div", { class: "money" }, input));
  }
  view.append(moneyCard);

  /* --- FOH hours --- */
  const hourEls = {};
  const fohHeader = el("h2", {}, "FOH — tippable hours");
  if (sq) fohHeader.append(makeBadge("foh_hours"));
  const fohCard = el("div", { class: "card" }, fohHeader);
  if (!foh.length) fohCard.append(el("div", { class: "note" }, "No FOH staff yet — add them on the Staff screen."));
  for (const e of foh) {
    const input = el("input", {
      type: "number", step: "0.01", min: "0", max: "24", inputmode: "decimal",
      value: inputs.foh_hours[e.id] ?? inputs.foh_hours[String(e.id)] ?? 0,
      ...(finalized ? { disabled: "" } : {}),
    });
    input.addEventListener("input", scheduleSave);
    const bump = (delta) => {
      input.value = Math.max(0, Math.min(24, (parseFloat(input.value) || 0) + delta)).toFixed(2).replace(/\.?0+$/, "") || "0";
      scheduleSave();
    };
    hourEls[e.id] = input;
    fohCard.append(el("div", { class: "staffrow" },
      el("span", { class: "name" }, e.display_name),
      el("div", { class: "hourctl" },
        el("button", { class: "ghost", type: "button", onclick: () => bump(-0.25), ...(finalized ? { disabled: "" } : {}) }, "−"),
        input,
        el("button", { class: "ghost", type: "button", onclick: () => bump(0.25), ...(finalized ? { disabled: "" } : {}) }, "+"))));
  }
  view.append(fohCard);

  /* --- BOH roster --- */
  const bohChecks = {};
  const bohHeader = el("h2", {}, "Kitchen — worked today");
  if (sq) bohHeader.append(makeBadge("boh_worked"));
  const bohCard = el("div", { class: "card" }, bohHeader);
  if (!boh.length) bohCard.append(el("div", { class: "note" }, "No BOH staff yet — add them on the Staff screen."));
  for (const e of boh) {
    const cb = el("input", {
      type: "checkbox",
      ...(inputs.boh_worked.includes(e.id) ? { checked: "" } : {}),
      ...(finalized ? { disabled: "" } : {}),
    });
    cb.addEventListener("change", scheduleSave);
    bohChecks[e.id] = cb;
    bohCard.append(el("label", { class: "check" }, cb, e.display_name));
  }
  view.append(bohCard);

  /* --- computed distribution --- */
  const computedCard = el("div", { class: "card" });
  view.append(computedCard);

  function renderComputed(c) {
    computedCard.textContent = "";
    computedCard.append(el("h2", {}, "Distribution"));
    for (const [flag, on] of Object.entries(c.flags)) {
      if (on) computedCard.append(el("div", { class: `flag ${flag === "negative_foh_pool" ? "bad" : ""}` }, FLAG_TEXT[flag] || flag));
    }
    computedCard.append(el("div", { class: "pools" },
      el("div", { class: "pool" }, el("div", { class: "v" }, fmt(c.totals.total_tips_cents)), el("div", { class: "k" }, "Total tips")),
      el("div", { class: "pool" }, el("div", { class: "v" }, fmt(c.totals.boh_allocation_cents)), el("div", { class: "k" }, "Kitchen share")),
      el("div", { class: "pool" }, el("div", { class: "v" }, fmt(c.totals.foh_pool_cents)), el("div", { class: "k" }, "FOH pool")),
      el("div", { class: "pool" }, el("div", { class: "v" }, fmt(c.totals.auto_gratuity_cents)), el("div", { class: "k" }, "Auto-gratuity")),
    ));
    if (c.foh.length) {
      const tbl = el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "FOH"), el("th", { class: "num" }, "Hrs"),
          el("th", { class: "num" }, "Tips"), el("th", { class: "num" }, "Grat"))),
        el("tbody", {}, c.foh.map((r) => el("tr", {},
          el("td", {}, esc(r.name)), el("td", { class: "num" }, r.hours),
          el("td", { class: "num" }, fmt(r.tips_cents)), el("td", { class: "num" }, fmt(r.gratuity_cents))))));
      computedCard.append(tbl);
    }
    if (c.boh.length) {
      computedCard.append(el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Kitchen"), el("th", { class: "num" }, "Share"))),
        el("tbody", {}, c.boh.map((r) => el("tr", {},
          el("td", {}, esc(r.name)), el("td", { class: "num" }, fmt(r.share_cents)))))));
    }
    actionTotal.textContent = fmt(c.totals.foh_pool_cents + c.totals.boh_allocation_cents + c.totals.auto_gratuity_cents);
    refreshBadges();
  }

  /* --- sticky action bar --- */
  const actionTotal = el("span", { class: "total" }, "");
  const actionBtn = finalized
    ? (ME.role === "admin"
        ? el("button", { class: "danger", onclick: async () => {
            if (!confirm("Reopen this finalized day? A new snapshot version will be written when it is finalized again.")) return;
            await api(`/api/days/${dateStr}/reopen`, { method: "POST" });
            route();
          } }, "Reopen")
        : el("span", { class: "hint" }, "Locked — ask an admin to reopen"))
    : el("button", { onclick: async () => {
        clearTimeout(saveTimer);
        await saveNow();
        if (!confirm(`Finalize ${dateStr}? Inputs lock and an immutable snapshot is saved.`)) return;
        try {
          await api(`/api/days/${dateStr}/finalize`, { method: "POST" });
          toast("Day finalized");
          route();
        } catch (e) { toast(e.message, true); }
      } }, "Finalize day");
  view.append(el("div", { class: "actionbar" },
    el("div", {}, el("div", {}, "Payout total: ", actionTotal), statusEl), actionBtn));

  renderComputed(day.computed);
}

/* ---------- period dashboard ---------- */


function poolTiles(t, model) {
  const spec = model === "PERCENT_TIPOUT"
    ? [[t.total_tips_cents, "Total tips"], [t.pool_busser_cents, "Busser pool"],
       [t.pool_host_cents, "Host pool"], [t.pool_boh_cents, "Kitchen (monthly)"],
       [t.auto_gratuity_cents, "Auto-gratuity"]]
    : [[t.total_tips_cents, "Total tips"], [t.boh_allocation_cents, "Kitchen share"],
       [t.foh_pool_cents, "FOH pool"], [t.auto_gratuity_cents, "Auto-gratuity"]];
  return el("div", { class: "pools" },
    ...spec.map(([v, k]) => el("div", { class: "pool" },
      el("div", { class: "v" }, fmt(v || 0)), el("div", { class: "k" }, k))));
}


const SCHEME_LABEL = {
  weekly: "Weekly · Fri–Thu (tip payout)",
  monthly: "Monthly (payroll)",
  semimonthly: "Semi-monthly",
};

function currentScheme(schemes) {
  const saved = sessionStorage.getItem("reportScheme:" + sessionStorage.getItem("venueId"));
  return schemes.includes(saved) ? saved : schemes[0];
}

function schemeToggle(p, rerender) {
  if ((p.schemes || []).length < 2) return null;
  return el("div", { class: "row", style: "margin:6px 0 10px;gap:8px" },
    ...p.schemes.map((s) => {
      const b = el("button", { class: `small ${s === p.scheme ? "" : "ghost"}`, type: "button" },
        SCHEME_LABEL[s] || s);
      b.addEventListener("click", () => {
        sessionStorage.setItem("reportScheme:" + sessionStorage.getItem("venueId"), s);
        rerender();
      });
      return b;
    }));
}

async function renderPeriod(anchorArg) {
  const anchor = anchorArg || ME.today;
  const saved = sessionStorage.getItem("reportScheme:" + sessionStorage.getItem("venueId"));
  const p = await api(`/api/periods/${anchor}${saved ? `?scheme=${saved}` : ""}`);
  sessionStorage.setItem("reportScheme:" + sessionStorage.getItem("venueId"), p.scheme);
  view.append(
    el("div", { class: "row spread" },
      el("button", { class: "ghost small", onclick: () => { location.hash = `#/period/${p.prev_anchor}`; } }, "‹"),
      el("h1", {}, `${p.start} → ${p.end}`),
      el("button", { class: "ghost small", onclick: () => { location.hash = `#/period/${p.next_anchor}`; } }, "›")),
  );

  const toggle = schemeToggle(p, () => route());
  if (toggle) view.append(toggle);
  view.append(poolTiles(p.totals, p.model));

  const daysCard = el("div", { class: "card" }, el("h2", {}, "Days"));
  for (const d of p.days) {
    daysCard.append(el("a", { class: "daychip", href: `#/day/${d.date}` },
      el("span", {},
        el("span", { class: "d" }, d.date.slice(5)),
        (d.flags_on || []).length ? el("span", { class: "warnmark", title: d.flags_on.join(", ") }, "⚠") : null),
      el("span", { class: "row" },
        d.total_tips_cents !== undefined ? el("span", { class: "amt" }, fmt(d.total_tips_cents)) : null,
        el("span", { class: `badge ${d.status}` }, d.status.replace("_", " ")))));
  }
  view.append(daysCard);

  if (p.employees.length) {
    const empCard = el("div", { class: "card" }, el("h2", {}, "Per-employee (drafts included)"));
    if (p.model === "PERCENT_TIPOUT") {
      empCard.append(el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "Employee"), el("th", { class: "num" }, "Keep"),
          el("th", { class: "num" }, "Pool/Ret"), el("th", { class: "num" }, "Tips"),
          el("th", { class: "num" }, "Grat"))),
        el("tbody", {}, p.employees.map((s) => el("tr", {},
          el("td", {}, esc(s.name)),
          el("td", { class: "num" }, fmt(s.keep_cents)),
          el("td", { class: "num" }, fmt(s.pool_share_cents + s.returned_cents)),
          el("td", { class: "num" }, fmt(s.tips_cents)),
          el("td", { class: "num" }, fmt(s.gratuity_cents)))))));
    } else {
      empCard.append(el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "Employee"), el("th", { class: "num" }, "Tips"),
          el("th", { class: "num" }, "Grat"), el("th", { class: "num" }, "Days"),
          el("th", { class: "num" }, "Hrs"))),
        el("tbody", {}, p.employees.map((s) => el("tr", {},
          el("td", {}, esc(s.name)),
          el("td", { class: "num" }, fmt(s.tips_cents + s.boh_cents)),
          el("td", { class: "num" }, fmt(s.gratuity_cents)),
          el("td", { class: "num" }, s.days),
          el("td", { class: "num" }, s.hours ? s.hours.toFixed(2) : "—"))))));
    }
    view.append(empCard);
  }

  view.append(el("div", { class: "row" },
    el("button", { onclick: () => { location.hash = `#/export/${p.start}`; } }, "Export this period")));
}

/* ---------- export ---------- */

async function renderExport(anchorArg) {
  const anchor = anchorArg || ME.today;
  const savedScheme = sessionStorage.getItem("reportScheme:" + sessionStorage.getItem("venueId"));
  const p = await api(`/api/periods/${anchor}/export${savedScheme ? `?scheme=${savedScheme}` : ""}`);
  sessionStorage.setItem("reportScheme:" + sessionStorage.getItem("venueId"), p.scheme);
  view.append(
    el("div", { class: "row spread" },
      el("button", { class: "ghost small", onclick: () => { location.hash = `#/export/${p.prev_anchor}`; } }, "‹"),
      el("h1", {}, `Export ${p.start} → ${p.end}`),
      el("button", { class: "ghost small", onclick: () => { location.hash = `#/export/${p.next_anchor}`; } }, "›")),
  );

  const expToggle = schemeToggle(p, () => route());
  if (expToggle) view.append(expToggle);
  if (p.draft_dates.length) {
    view.append(el("div", { class: "flag" },
      `${p.draft_dates.length} day(s) not finalized and excluded from this export: ${p.draft_dates.join(", ")}`));
  }
  if (p.flagged_dates.length) {
    view.append(el("div", { class: "flag" },
      `Flagged days need review: ${p.flagged_dates.join(", ")}`));
  }

  // per-period editable cash payout (LF): pre-filled to the next amount
  // ending in zero; edits persist for this period + scheme
  async function saveCashPayout(employeeId, cents) {
    try {
      await api(`/api/periods/${p.start}/cash-payouts?scheme=${p.scheme}`,
        { method: "PUT", body: { payouts: { [employeeId]: cents } } });
      route();
    } catch (e) { toast(e.message, true); }
  }
  function cashInput(employeeId, cents) {
    const input = el("input", { inputmode: "decimal", type: "text",
      value: (cents / 100).toFixed(2),
      style: "width:84px;text-align:right;min-height:36px;padding:6px" });
    input.addEventListener("blur", () => {
      const v = centsFromInput(input);
      if (v !== cents) saveCashPayout(employeeId, v);
    });
    input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") input.blur(); });
    return input;
  }

  const card = el("div", { class: "card" }, el("h2", {}, "Payroll totals (finalized days only)"));
  if (p.model === "PERCENT_TIPOUT") {
    const weekly = p.scheme === "weekly";
    card.append(el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Employee"), el("th", { class: "num" }, "Keep"),
        el("th", { class: "num" }, "Pool/Ret"), el("th", { class: "num" }, "Tips"),
        ...(weekly ? [el("th", { class: "num" }, "Cash pay"),
                      el("th", { class: "num" }, "↑")] : []),
        el("th", { class: "num" }, "Auto-grat"))),
      el("tbody", {}, p.employees.map((s) => el("tr", {},
        el("td", {}, esc(s.name)),
        el("td", { class: "num" }, fmt(s.keep_cents)),
        el("td", { class: "num" }, fmt(s.pool_share_cents + s.returned_cents)),
        el("td", { class: "num" }, fmt(s.tips_cents)),
        ...(weekly ? [el("td", { class: "num" }, cashInput(s.employee_id, s.cash_payout_cents)),
                      el("td", { class: "num" }, fmt(s.roundup_cents))] : []),
        el("td", { class: "num" }, fmt(s.gratuity_cents)))))));
    if (weekly) {
      // the bank-withdrawal number — headline, not a footnote
      view.append(el("div", { class: "hero" },
        el("div", { class: "k" }, "Cash to pay out"),
        el("div", { class: "v" }, fmt(p.totals.total_cash_payout_cents || 0)),
        el("div", { class: "sub" },
          `total round-up ${fmt(p.totals.total_roundup_cents || 0)} · payroll month report stays exact`)));
    }
  } else {
    card.append(el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Employee"), el("th", { class: "num" }, "Tips"),
        el("th", { class: "num" }, "Auto-grat"), el("th", { class: "num" }, "Days"),
        el("th", { class: "num" }, "Hrs"))),
      el("tbody", {}, p.employees.map((s) => el("tr", {},
        el("td", {}, esc(s.name)),
        el("td", { class: "num" }, fmt(s.tips_cents + s.boh_cents)),
        el("td", { class: "num" }, fmt(s.gratuity_cents)),
        el("td", { class: "num" }, s.days),
        el("td", { class: "num" }, s.hours ? s.hours.toFixed(2) : "—"))))));
  }
  view.append(card);

  /* --- LF monthly kitchen payout: roster decided here (ruling 2026-07-06) --- */
  if (p.boh_monthly) {
    const bm = p.boh_monthly;
    const kCard = el("div", { class: "card" },
      el("h2", {}, "Kitchen monthly payout"),
      el("div", { class: "note" },
        `Month's kitchen pool: ${fmt(bm.allocation_cents)} — split evenly among the checked staff (pre-filled from who worked). Saved per month, audit-logged.`));
    if (bm.unassigned) {
      kCard.append(el("div", { class: "flag bad" },
        "Kitchen pool has money but nobody selected — check who should share it."));
    }
    const selected = new Set(bm.members.filter((m) => m.selected).map((m) => m.employee_id));
    const grid = el("div", { class: "bohgrid" });
    for (const m of bm.members) {
      const box = el("span", { class: "box" }, m.selected ? "✓" : "");
      const share = bm.shares[String(m.employee_id)];
      const chip = el("button", { class: `chip ${m.selected ? "on" : ""}`, type: "button" },
        box, el("span", {}, m.name,
          el("div", { class: "hint" },
            (m.worked_days ? `${m.worked_days} day(s) worked` : "no pulled days")
            + (share !== undefined ? ` · ${fmt(share)}` : ""))));
      chip.addEventListener("click", async () => {
        selected.has(m.employee_id) ? selected.delete(m.employee_id) : selected.add(m.employee_id);
        try {
          await api(`/api/periods/${p.start}/boh-roster`,
            { method: "PUT", body: { employee_ids: [...selected] } });
          route();  // re-render with fresh shares
        } catch (e) { toast(e.message, true); }
      });
      grid.append(chip);
    }
    kCard.append(grid);
    const paid = bm.members.filter((m) => m.share_cents !== undefined);
    if (paid.length) {
      kCard.append(el("div", { class: "seclabel" }, "Cash payouts (rounded)"));
      for (const m of paid) {
        kCard.append(el("div", { class: "hrow" },
          el("div", { class: "who" }, el("div", { class: "nm" }, m.name),
            el("div", { class: "sub" }, `share ${fmt(m.share_cents)} · round-up ${fmt(m.roundup_cents)}`)),
          el("div", { style: "flex:none" }, cashInput(m.employee_id, m.cash_payout_cents))));
      }
      kCard.append(el("div", { class: "hero", style: "margin-top:12px" },
        el("div", { class: "k" }, "Kitchen cash to pay"),
        el("div", { class: "v" }, fmt(bm.total_cash_payout_cents)),
        el("div", { class: "sub" }, `total round-up ${fmt(bm.total_roundup_cents)}`)));
    }
    view.append(kCard);
  }

  const dl = el("button", {}, "Download CSV");
  dl.addEventListener("click", async () => {
    const headers = {};
    const vid = sessionStorage.getItem("venueId");
    if (vid) headers["X-Venue-Id"] = vid;
    const res = await fetch(`/api/periods/${p.start}/export.csv?scheme=${p.scheme}`, { headers });
    if (!res.ok) { toast("export failed", true); return; }
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (res.headers.get("content-disposition") || "").match(/filename="(.+)"/)?.[1]
      || `tips_${p.start}_${p.end}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  });
  view.append(el("div", { class: "row" }, dl));
}

/* ---------- users (super admin) ---------- */

async function renderUsers() {
  if (!ME.super_admin) { location.hash = "#/day"; return; }
  const [users, venues] = await Promise.all([api("/api/users"), api("/api/venues")]);
  view.append(el("h1", {}, "Users & venue access"));

  function venuePicker(selectedIds, disabled = false) {
    const selected = new Set((selectedIds || []).map(Number));
    const checks = {};
    const wrap = el("div", { class: "grid2" });
    for (const v of venues) {
      const cb = el("input", { type: "checkbox",
        ...(selected.has(v.id) ? { checked: "" } : {}),
        ...(disabled ? { disabled: "" } : {}) });
      checks[v.id] = cb;
      wrap.append(el("label", { class: "check" }, cb, v.name));
    }
    return {
      wrap,
      values: () => Object.entries(checks)
        .filter(([, cb]) => cb.checked)
        .map(([id]) => Number(id)),
    };
  }

  const addEmail = el("input", { type: "email", placeholder: "email" });
  const addPass = el("input", { type: "password", placeholder: "temporary password" });
  const addRole = el("select", {},
    el("option", { value: "manager" }, "Manager"),
    el("option", { value: "admin" }, "Admin"));
  const addSuper = el("input", { type: "checkbox" });
  const addVenues = venuePicker([ME.venue.id]);
  const addBtn = el("button", {}, "Create user");
  addBtn.addEventListener("click", async () => {
    try {
      await api("/api/users", { method: "POST", body: {
        email: addEmail.value.trim(),
        password: addPass.value,
        role: addRole.value,
        super_admin: addSuper.checked,
        venue_ids: addVenues.values(),
      } });
      toast("User created");
      route();
    } catch (e) { toast(e.message, true); }
  });
  view.append(el("div", { class: "card" },
    el("h2", {}, "Add user"),
    el("label", {}, "Email"), addEmail,
    el("label", {}, "Temporary password"), addPass,
    el("label", {}, "Role for selected venues"), addRole,
    el("label", { class: "check" }, addSuper, "Super Admin (all venues)"),
    el("label", {}, "Venue access"), addVenues.wrap,
    el("div", { style: "margin-top:12px" }, addBtn)));

  for (const u of users) {
    const self = u.id === ME.id;
    const selected = u.super_admin ? venues.map((v) => v.id) : u.access.map((a) => a.venue_id);
    const accessText = u.super_admin
      ? "All venues"
      : (u.access || []).map((a) => a.name).join(", ") || "No venues";
    const roleSel = el("select", { style: "width:auto" },
      el("option", { value: "manager", ...(u.role === "manager" ? { selected: "" } : {}) }, "Manager"),
      el("option", { value: "admin", ...(u.role === "admin" ? { selected: "" } : {}) }, "Admin"));
    const active = el("input", { type: "checkbox", ...(u.active ? { checked: "" } : {}),
      ...(self ? { disabled: "" } : {}) });
    const superBox = el("input", { type: "checkbox", ...(u.super_admin ? { checked: "" } : {}),
      ...(self ? { disabled: "" } : {}) });
    const vp = venuePicker(selected, u.super_admin);
    const pass = el("input", { type: "password", placeholder: "new password (optional)" });
    const save = el("button", { class: "small" }, "Save user");
    save.addEventListener("click", async () => {
      const body = {
        role: roleSel.value,
        active: active.checked,
        super_admin: superBox.checked,
        venue_ids: vp.values(),
      };
      if (pass.value) body.password = pass.value;
      try {
        await api(`/api/users/${u.id}`, { method: "PATCH", body });
        toast("User saved");
        route();
      } catch (e) { toast(e.message, true); }
    });
    view.append(el("details", { class: "card usercard" },
      el("summary", {},
        el("div", { class: "usersummary" },
          el("div", { class: "uemail" }, u.email),
          el("div", { class: "umeta" },
            u.super_admin ? "Super Admin" : roleSel.value[0].toUpperCase() + roleSel.value.slice(1),
            " · ",
            accessText)),
        el("span", { class: `badge ${u.active ? "finalized" : "draft"}` },
          u.active ? "active" : "inactive")),
      el("div", { class: "staffrow" },
        el("span", { class: "name" }, "Role"), roleSel),
      el("label", { class: "check" }, active, "Active"),
      el("label", { class: "check" }, superBox, "Super Admin (all venues)"),
      el("label", {}, "Venue access"), vp.wrap,
      el("label", {}, "Reset password"), pass,
      el("div", { style: "margin-top:12px" }, save),
      self ? el("div", { class: "note" },
        "You cannot deactivate yourself or remove your own Super Admin access.") : null));
  }
}

/* ---------- audit log ---------- */

async function renderAudit() {
  if (ME.role !== "admin") { location.hash = "#/day"; return; }
  const allKey = "auditAllVenues";
  const all = ME.super_admin && sessionStorage.getItem(allKey) === "1";
  const rows = await api(`/api/audit-log?limit=250${all ? "&all_venues=true" : ""}`);
  view.append(el("div", { class: "row spread" },
    el("h1", {}, all ? "Audit — all venues" : `Audit — ${ME.venue.name}`),
    ME.super_admin ? (() => {
      const cb = el("input", { type: "checkbox", ...(all ? { checked: "" } : {}) });
      cb.addEventListener("change", () => {
        sessionStorage.setItem(allKey, cb.checked ? "1" : "0");
        route();
      });
      return el("label", { class: "check", style: "margin:0" }, cb, "All venues");
    })() : null));
  const card = el("div", { class: "card auditcard" });
  if (!rows.length) {
    card.append(el("div", { class: "note" }, "No audit entries yet."));
  } else {
    card.append(el("table", { class: "audittable" },
      el("thead", {}, el("tr", {},
        el("th", {}, "When"), el("th", {}, "Venue"), el("th", {}, "User"),
        el("th", {}, "Action"), el("th", {}, "Target"), el("th", {}, "Details"))),
      el("tbody", {}, rows.map((r) => {
        let detail = r.detail_json || "";
        try { detail = JSON.stringify(JSON.parse(detail), null, 1); } catch {}
        // venue-local time, two tidy lines (stored timestamps are UTC)
        const when = new Date(r.ts.includes("+") || r.ts.endsWith("Z") ? r.ts : r.ts + "Z");
        const dateStr = when.toLocaleDateString("en-US", {
          timeZone: ME.venue.timezone, month: "short", day: "numeric" });
        const timeStr = when.toLocaleTimeString("en-US", {
          timeZone: ME.venue.timezone, hour: "numeric", minute: "2-digit" });
        return el("tr", {},
          el("td", { class: "auditwhen", "data-label": "When" },
            el("div", {},
              el("span", { class: "d" }, dateStr),
              el("span", { class: "t" }, timeStr))),
          el("td", { "data-label": "Venue" }, r.venue_name),
          el("td", { "data-label": "User" }, (r.user_email || "system").split("@")[0]),
          el("td", { "data-label": "Action" },
            el("span", { class: "auditaction" }, r.action.replace(/_/g, " "))),
          el("td", { "data-label": "Target" }, `${r.entity}${r.entity_id ? ` #${r.entity_id}` : ""}`),
          el("td", { class: "auditdetail", "data-label": "Details" }, detail));
      }))));
  }
  view.append(card);
}

/* ---------- employees (admin) ---------- */

async function renderEmployees() {
  if (ME.role !== "admin") { location.hash = "#/day"; return; }
  const employees = await api("/api/employees");
  view.append(el("h1", {}, `Staff — ${ME.venue.name}`));

  const isLF = ME.venue.tip_model === "PERCENT_TIPOUT";
  const groups = isLF
    ? { SERVER: "Servers", BUSSER: "Bussers", HOST: "Hosts", BOH: "Kitchen",
        EXCLUDED: "Managers / owners (no pools)" }
    : { FOH: "Front of house", BOH: "Kitchen", EXCLUDED: "Managers / owners (no pools)" };
  const roleOptions = Object.keys(groups);
  for (const [role, title] of Object.entries(groups)) {
    const card = el("div", { class: "card" }, el("h2", {}, title));
    for (const e of employees.filter((x) => x.pool_role === role)) {
      const roleSel = el("select", { style: "width:auto" },
        ...roleOptions.map((r) =>
          el("option", { value: r, ...(r === e.pool_role ? { selected: "" } : {}) }, r)));
      roleSel.addEventListener("change", async () => {
        await api(`/api/employees/${e.id}`, { method: "PATCH", body: { pool_role: roleSel.value } });
        toast(`${e.display_name} → ${roleSel.value}`);
        route();
      });
      // round-up moved to the export screens (per period); Staff keeps only
      // the durable person attribute: salaried kitchen always in the pool
      let poolFlag = null;
      if (isLF && role === "BOH") {
        poolFlag = el("button", {
          class: `small ${e.always_in_boh_pool ? "" : "ghost"}`, type: "button",
          title: "Salaried — pre-selected in the monthly kitchen pool even without timecards",
        }, e.always_in_boh_pool ? "★ always in pool" : "☆ always in pool");
        poolFlag.addEventListener("click", async () => {
          await api(`/api/employees/${e.id}`, { method: "PATCH",
            body: { always_in_boh_pool: !e.always_in_boh_pool } });
          toast(`${e.display_name} ${e.always_in_boh_pool ? "no longer" : "now"} always in the kitchen pool`);
          route();
        });
      }
      const activeBtn = el("button", { class: "ghost small" }, e.active ? "Deactivate" : "Activate");
      activeBtn.addEventListener("click", async () => {
        await api(`/api/employees/${e.id}`, { method: "PATCH", body: { active: !e.active } });
        route();
      });
      card.append(el("div", { class: "staffrow" },
        el("span", { class: "name", style: e.active ? "" : "opacity:.4" },
          e.display_name,
          e.square_team_member_id
            ? el("span", { class: "src square", title: e.square_team_member_id }, "Square")
            : null),
        el("div", { class: "row" }, ...(poolFlag ? [poolFlag] : []), roleSel, activeBtn)));
    }
    view.append(card);
  }

  const name = el("input", { placeholder: "Name" });
  const role = el("select", {},
    ...roleOptions.map((r) =>
      el("option", { value: r }, r === "EXCLUDED" ? "EXCLUDED (manager/owner)" : r)));
  const addBtn = el("button", {}, "Add");
  addBtn.addEventListener("click", async () => {
    if (!name.value.trim()) return;
    try {
      await api("/api/employees", { method: "POST", body: { display_name: name.value.trim(), pool_role: role.value } });
      toast("Added");
      route();
    } catch (e) { toast(e.message, true); }
  });
  view.append(el("div", { class: "card" },
    el("h2", {}, "Add employee"),
    el("label", {}, "Name"), name,
    el("label", {}, "Pool role"), role,
    el("div", { style: "margin-top:12px" }, addBtn),
    el("div", { class: "note" }, "EXCLUDED staff are hard-blocked from every pool (WA law). Days that reference them will refuse to compute.")));
}

/* ---------- settings / setup (admin, M3) ---------- */

const mm2hhmm = (m) =>
  `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const hhmm2mm = (s) => {
  const m = /^(\d{1,2}):(\d{2})$/.exec(s.trim());
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
};
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

async function renderSettings() {
  if (ME.role !== "admin") { location.hash = "#/day"; return; }
  const [s, employees] = await Promise.all([api("/api/settings"), api("/api/employees")]);
  const isLF = ME.venue.tip_model === "PERCENT_TIPOUT";
  view.append(el("h1", {}, `Setup — ${ME.venue.name}`));

  /* --- connection --- */
  view.append(el("div", { class: "card" },
    el("h2", {}, "Square connection"),
    el("div", {}, s.square.configured
      ? `Connected (${s.square.env}, ${s.square.location_ids.length > 1 ? "locations" : "location"} ${s.square.location_ids.join(", ")}) — all locations pool together`
      : "Not configured — set SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID in .env and restart."),
    el("div", { class: "note" }, s.square.configured
      ? (s.square.nightly_sync
          ? `Nightly auto-sync pulls the prior day at ${s.square.nightly_sync_hour}:00.`
          : "Nightly auto-sync is off (NIGHTLY_SYNC=0).")
      : "")));

  /* --- categories (collapsible; opens itself when mapping needs attention) --- */
  const catIdsAll = Object.keys(s.category_map);
  const unmappedCount = catIdsAll.filter((cid) => s.category_map[cid].group === null).length;
  const catCard = el("details", { class: "card", ...(unmappedCount ? { open: "" } : {}) },
    el("summary", {},
      el("h2", {}, "Category mapping",
        unmappedCount
          ? el("span", { class: "src blocked" }, `${unmappedCount} unmapped`)
          : el("span", { class: "src manual" }, `${catIdsAll.length}`))));
  const syncCatBtn = el("button", { class: "ghost small" }, "⟳ Sync categories from Square");
  syncCatBtn.addEventListener("click", async () => {
    try {
      const out = await api("/api/square/sync-catalog", { method: "POST" });
      toast(`${out.total} categories, ${out.unmapped} unmapped`);
      route();
    } catch (e) { toast(e.message, true); }
  });
  catCard.append(el("div", { style: "margin-bottom:8px" }, syncCatBtn));
  const catIds = Object.keys(s.category_map).sort(
    (a, b) => s.category_map[a].name.localeCompare(s.category_map[b].name));
  if (!catIds.length) {
    catCard.append(el("div", { class: "note" }, "No categories yet — sync from Square."));
  }
  for (const cid of catIds) {
    const entry = s.category_map[cid];
    const sel = el("select", { style: "width:auto" },
      el("option", { value: "" }, "— unmapped —"),
      ...s.category_groups.map((g) =>
        el("option", { value: g, ...(entry.group === g ? { selected: "" } : {}) }, g)));
    sel.addEventListener("change", async () => {
      s.category_map[cid] = { ...entry, group: sel.value || null };
      try {
        await api("/api/settings", { method: "PUT", body: { category_map: s.category_map } });
        toast(`${entry.name} → ${sel.value || "unmapped"}`);
      } catch (e) { toast(e.message, true); }
    });
    catCard.append(el("div", { class: "staffrow" },
      el("span", { class: "name" }, entry.name,
        entry.group === null ? el("span", { class: "src blocked" }, "unmapped") : null),
      sel));
  }
  if (!isLF) view.append(catCard);  // no food-sales carve-out in PERCENT_TIPOUT

  /* --- LF tip-out percentages (PERCENT_TIPOUT venues only) --- */
  if (isLF) {
    const pctCard = el("div", { class: "card" },
      el("h2", {}, "Tip-out percentages"),
      el("div", { class: "note" },
        "Of each server's OWN tips. Must sum to exactly 100. Pools split evenly among who worked (owner ruling)."));
    const pctEls = {};
    for (const key of ["server", "busser", "host", "boh"]) {
      const input = el("input", { inputmode: "decimal", value: s.lf_percentages[key],
                                  style: "width:86px;text-align:right" });
      pctEls[key] = input;
      pctCard.append(el("div", { class: "staffrow" },
        el("span", { class: "name" }, key === "boh" ? "Kitchen" :
          key[0].toUpperCase() + key.slice(1) + (key === "server" ? " keeps" : " pool")),
        el("div", { class: "row" }, input, el("span", { class: "hint" }, "%"))));
    }
    const saveBtn = el("button", { class: "small" }, "Save percentages");
    saveBtn.addEventListener("click", async () => {
      const body = Object.fromEntries(
        Object.entries(pctEls).map(([k, i]) => [k, i.value.trim()]));
      try {
        await api("/api/settings", { method: "PUT", body: { lf_percentages: body } });
        toast("Percentages saved");
      } catch (e) { toast(e.message, true); }
    });
    pctCard.append(el("div", { style: "margin-top:10px" }, saveBtn));
    view.append(pctCard);

    const thrInput = el("input", { type: "number", min: "0", max: "20",
      inputmode: "numeric", value: s.lf_no_host_min_bussers,
      style: "width:80px;text-align:right" });
    thrInput.addEventListener("blur", async () => {
      const n = parseInt(thrInput.value, 10);
      if (!Number.isFinite(n) || n < 0) { toast("enter a number", true); return; }
      try {
        await api("/api/settings", { method: "PUT",
          body: { lf_no_host_min_bussers: n } });
        toast("No-host flag threshold saved");
      } catch (e) { toast(e.message, true); }
    });
    view.append(el("div", { class: "card" },
      el("h2", {}, "No-host day flag"),
      el("div", { class: "row" },
        el("span", { class: "hint", style: "flex:1" },
          "Flag a no-host day only when fewer than this many bussers worked (the 10%-to-bussers re-split always applies; 0 = never flag):"),
        thrInput)));
  }

  /* --- gratuity service charge --- */
  const gratInput = el("input", { value: s.gratuity_service_charge.name_contains || "" });
  const gratSave = el("button", { class: "small" }, "Save");
  gratSave.addEventListener("click", async () => {
    try {
      await api("/api/settings", { method: "PUT", body: {
        gratuity_service_charge: { catalog_object_id: null,
                                   name_contains: gratInput.value.trim() } } });
      toast("Saved");
    } catch (e) { toast(e.message, true); }
  });
  view.append(el("div", { class: "card" },
    el("h2", {}, "Auto-gratuity service charge"),
    el("div", { class: "note" }, "Square catalog gratuity charges (type AUTO_GRATUITY) are detected automatically, pre-tax. The name match below additionally catches custom/ad-hoc charges."),
    el("label", {}, "Also match service charges whose name contains"),
    el("div", { class: "row" }, gratInput, gratSave),
    el("div", { class: "note" }, "Matched charges are pulled as the auto-gratuity pool (reported as wages, separate from tips).")));

  /* --- tippable windows --- */
  const winCard = el("div", { class: "card" },
    el("h2", {}, "Tippable window (open to public)"),
    el("div", { class: "note" }, "Hours outside this window never earn tip-pool shares. 24:00 = midnight (hard cutoff)."));
  for (let wd = 0; wd < 7; wd++) {
    const w = s.tippable_windows[String(wd)];
    const openIn = el("input", { value: mm2hhmm(w.open_minutes), style: "width:86px" });
    const closeIn = el("input", { value: mm2hhmm(w.close_minutes), style: "width:86px" });
    const save = async () => {
      const open = hhmm2mm(openIn.value), close = hhmm2mm(closeIn.value);
      if (open === null || close === null) { toast("Use HH:MM", true); return; }
      try {
        await api("/api/settings", { method: "PUT", body: {
          tippable_windows: { [String(wd)]: { open_minutes: open, close_minutes: close } } } });
        toast(`${WEEKDAYS[wd]} window saved`);
      } catch (e) { toast(e.message, true); }
    };
    openIn.addEventListener("blur", save);
    closeIn.addEventListener("blur", save);
    winCard.append(el("div", { class: "staffrow" },
      el("span", { class: "name" }, WEEKDAYS[wd]),
      el("div", { class: "row" }, openIn, el("span", { class: "hint" }, "to"), closeIn)));
  }
  view.append(winCard);

  /* --- business day boundary --- */
  const cutoffIn = el("input", { value: mm2hhmm(s.day_cutoff_minutes), style: "width:86px" });
  cutoffIn.addEventListener("blur", async () => {
    const m = hhmm2mm(cutoffIn.value);
    if (m === null || m > 360) { toast("Use HH:MM, up to 06:00", true); return; }
    try {
      await api("/api/settings", { method: "PUT", body: { day_cutoff_minutes: m } });
      toast("Day boundary saved — re-pull affected days");
    } catch (e) { toast(e.message, true); }
  });
  view.append(el("div", { class: "card" },
    el("h2", {}, "Business day ends at"),
    el("div", { class: "row" }, cutoffIn, el("span", { class: "hint" }, "after midnight (00:00 = midnight)")),
    el("div", { class: "note" },
      "Square pulls cover the service day up to this time — late check settlements after midnight stay on the prior day. This does NOT extend the tippable window, which still ends at midnight.")));

  /* --- warning muting --- */
  const WARNING_LABELS = {
    missing_clockout: "Missing clock-out — FOH hours skipped for that shift",
    all_cash_tips_zero: "Every declared cash tip is $0",
    uncataloged_line_items: "Custom-amount sales not counted as food",
  };
  const warnCard = el("div", { class: "card" },
    el("h2", {}, "Pull warnings"),
    el("div", { class: "note" },
      "Unchecked warnings stay hidden on the Daily screen. Blocking issues (unmapped categories or staff) can never be hidden."));
  const muted = new Set(s.muted_warnings || []);
  for (const [code, label] of Object.entries(WARNING_LABELS)) {
    const cb = el("input", { type: "checkbox", ...(muted.has(code) ? {} : { checked: "" }) });
    cb.addEventListener("change", async () => {
      cb.checked ? muted.delete(code) : muted.add(code);
      try {
        await api("/api/settings", { method: "PUT", body: { muted_warnings: [...muted] } });
        toast(cb.checked ? "Warning enabled" : "Warning hidden");
      } catch (e) { toast(e.message, true); }
    });
    warnCard.append(el("label", { class: "check" }, cb, label));
  }
  view.append(warnCard);

  /* --- team linking (collapsible; opens itself when links are missing) --- */
  const linkedTmidsPre = new Set(employees.flatMap((e) => e.square_team_member_ids || []));
  const teamAll = s.square_team_cache || [];
  const unlinkedCount = teamAll.filter((tm) => !linkedTmidsPre.has(tm.id)).length;
  const teamCard = el("details", { class: "card", ...(unlinkedCount ? { open: "" } : {}) },
    el("summary", {},
      el("h2", {}, "Square team members",
        unlinkedCount
          ? el("span", { class: "src blocked" }, `${unlinkedCount} unlinked`)
          : el("span", { class: "src manual" }, `${teamAll.length}`))));
  const syncTeamBtn = el("button", { class: "ghost small" }, "⟳ Sync team from Square");
  syncTeamBtn.addEventListener("click", async () => {
    try {
      const out = await api("/api/square/sync-team", { method: "POST" });
      toast(`${out.team.length} team members, ${out.unlinked.length} unlinked`);
      route();
    } catch (e) { toast(e.message, true); }
  });
  teamCard.append(el("div", { style: "margin-bottom:8px" }, syncTeamBtn));
  const linkedTmids = new Set(employees.flatMap((e) => e.square_team_member_ids || []));
  // a person can hold several Square accounts — every active employee is linkable
  const linkableEmps = employees.filter((e) => e.active);
  const team = s.square_team_cache || [];
  if (!team.length) {
    teamCard.append(el("div", { class: "note" }, "No team cache yet — sync from Square."));
  }
  for (const tm of team) {
    if (linkedTmids.has(tm.id)) {
      const emp = employees.find((e) => (e.square_team_member_ids || []).includes(tm.id));
      teamCard.append(el("div", { class: "staffrow" },
        el("span", { class: "name" }, tm.name),
        el("span", { class: "src square" }, `→ ${emp.display_name}`)));
      continue;
    }
    const venueRoles = isLF
      ? ["SERVER", "BUSSER", "HOST", "BOH", "EXCLUDED"]
      : ["FOH", "BOH", "EXCLUDED"];
    const sel = el("select", { style: "width:auto" },
      el("option", { value: "" }, "link to…"),
      ...linkableEmps.map((e2) => el("option", { value: e2.id },
        (e2.square_team_member_ids || []).length
          ? `${e2.display_name} (add 2nd account)` : e2.display_name)),
      ...venueRoles.map((r) => el("option", { value: `new:${r}` }, `＋ create as ${r}`)));
    sel.addEventListener("change", async () => {
      if (!sel.value) return;
      try {
        if (sel.value.startsWith("new:")) {
          await api("/api/employees", { method: "POST", body: {
            display_name: tm.name, pool_role: sel.value.slice(4),
            square_team_member_id: tm.id } });
        } else {
          await api(`/api/employees/${sel.value}`, { method: "PATCH", body: {
            square_team_member_id: tm.id } });
        }
        toast(`${tm.name} linked`);
        route();
      } catch (e) { toast(e.message, true); }
    });
    teamCard.append(el("div", { class: "staffrow" },
      el("span", { class: "name" }, tm.name,
        el("span", { class: "src blocked" }, "unlinked")),
      sel));
  }
  view.append(teamCard);
}

/* ---------- boot ---------- */
route();
