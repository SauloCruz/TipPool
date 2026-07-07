# Milestone 5 — Multi-Venue Support + La Fontana Tip Model

> Add this to the repo (e.g., `docs/M5-la-fontana.md`) and prompt Claude Code:
> "Read CLAUDE.md and docs/M5-la-fontana.md. Implement Milestone 5."
> Append the Owner Decisions in §6 to the CLAUDE.md Owner Decisions Log.

## 0. Prime directive

Tavern Law production must not wobble. All 215 existing tests stay green,
untouched. The TL engine, its golden fixtures, and its finalized data are
frozen — this milestone ADDS a venue and a second calculation model beside
them. TL and Needle & Thread remain ONE venue in this app ("Tavern Law"),
as today. The mid-July parallel run happens on this codebase, so keep
changes additive and behind venue selection.

## 1. What's being added

**La Fontana Siciliana** — separate restaurant, separate Square merchant
account, completely different tip model. The owner runs payroll for all
venues from this one app.

- App start: **venue picker** (dropdown/cards) before anything else; the
  selected venue scopes every screen, query, and export. Prominent venue
  name in the header at all times — the #1 operator error to prevent is
  finalizing a day against the wrong restaurant.
- All users see all venues for now. **RBAC per venue is deferred** — but
  add `user_venue_access` (user_id, venue_id, role) to the schema now,
  unenforced, so enabling it later is a query change, not a migration.
- Venue switching preserves nothing across venues: day drafts, settings,
  mappings, exports are venue-scoped. Cross-venue reporting is explicitly
  out of scope.

## 2. Engine architecture — strategy per venue

Refactor the engine entry point to dispatch on the venue's `tip_model`:

- `POOL_HOURS` — existing Tavern Law model. Zero behavior change.
  Extraction into the strategy interface must be a pure refactor proven
  by the untouched test suite.
- `PERCENT_TIPOUT` — new La Fontana model (§3).

Shared across models: integer-cents math, largest-remainder rounding,
conservation invariants, immutable finalize snapshots, audit logging,
provenance badges, override flow. These are platform, not model, features.

## 3. La Fontana tip model — PERCENT_TIPOUT

### Roles (fixed per PERSON — Owner ruling)
`SERVER`, `BUSSER`, `HOST`, `BOH`, `EXCLUDED` (managers — hard-blocked
from every pool, same rule as TL). At La Fontana, roles are stable:
servers are always servers, bussers always bussers, etc. Role is a
per-employee attribute set on the employee mapping screen (seeded from
their Square job title, admin-editable). Multi-job/per-day role handling
is OUT of v1 scope — if a timecard's job title ever conflicts with the
assigned role, show a warning and use the assigned role.

### Daily inputs
| Field | Source |
|---|---|
| `server_tips[s]` | Square (auto) — per-server credit tips: sum of `tip_money` on payments attributed to server s (`team_member_id`), that business day |
| `server_cash_tips[s]` | Square (auto) — `declared_cash_tip_money` from server s's timecard. Policy starts soon; until servers declare, this is 0. Build it NOW so day one of the policy just works. Same all-zero-day warning as TL. |
| `auto_gratuity` | Square (auto) — service charges, if any. Same handling as TL: separate pool, separate payroll line (wages, not tips). Distribution: hours-proportional across SERVER+BUSSER+HOST hours. |
| `worked[role]` | Square timecards (auto) — who worked, hours, per role |

### Calculation (per server s, then pooled)
```
tips[s]        = server_tips[s] + server_cash_tips[s]      # cents

server_keep[s] = 65% of tips[s]
busser_pool   += 20% of tips[s]
host_pool     += 10% of tips[s]
boh_pool      +=  5% of tips[s]
```
Percentages are venue settings with effective dates (65/20/10/5 defaults);
must sum to 100% — validate at settings save.

Pool distribution (Owner ruling — single shift per day, so daily pools):
- `busser_pool` → **EVEN SPLIT** among bussers who worked that day (default)
- `host_pool`   → **EVEN SPLIT** among hosts who worked that day (default)
- `boh_pool`    → **EVEN SPLIT** among BOH who worked that day (default)
- Keep an `HOURS_PROPORTIONAL` per-pool config toggle for future use;
  ships OFF everywhere.

**Empty-pool rules (Owner rulings):**
- **No host worked:** each server's 10% host share is re-split
  **75% back to that server / 20% to the busser pool / 5% to the BOH
  pool** (effective day percentages: server 72.5, bussers 22, BOH 5.5 —
  must still conserve to the cent). Day flagged: "No host — 10% re-split
  75/20/5 per policy."
- **No bussers or no BOH worked** (rare): that pool returns pro-rata to
  the contributing servers; day flagged. (Owner has not defined a
  re-split for these cases; do not invent one.)
- Cascade order: apply the no-host re-split FIRST, then the empty-pool
  return for any pool still without recipients.

Per-server payout on the payroll export:
```
payout[s]     = server_keep[s] + any returned-pool share
payout[b/h/k] = pool shares (+ server_keep if they also served that day)
gratuity line = separate, per §2
```

### Conservation invariants (test hard)
- Σ all payouts (keep + pools + returns) == Σ tips[s] exactly, in cents
- Σ per-pool distributions == pool total
- Rounding residuals resolved largest-remainder inside each pool;
  document tie-break (largest contribution/hours, then name)

### Tippable hours
No tippable-window clipping at La Fontana v1 — pools split on full
timecard hours (minus unpaid breaks), exact-minute decimals per the
existing Owner ruling (min ÷ 60, 2-decimal display, NO steppers, NO
quarter-hour rounding). Window clipping stays available as a venue
setting, off by default here.

## 4. Square integration deltas

- **Per-venue Square credentials.** La Fontana is a DIFFERENT merchant
  account: separate access token, location ID(s), category and team
  mappings. Extend settings + `.env` handling to per-venue credential
  sets; never mix tokens across venues; audit-log which venue each sync
  ran for.
- **Per-server tip attribution** is new: TL pools all tips so attribution
  never mattered; here `tip_money` must map to the server via the
  payment/order `team_member_id`. Handle the unattributed case (no
  team member on the payment — counter sale, house account): surface an
  "Unattributed tips: $X" bucket on Daily Review that BLOCKS finalize
  until the manager assigns it to a server (or marks it house/no-tip),
  with audit log. Never silently assign.
- Food-sales category mapping is NOT needed for the LF model (no
  food-sales carve-out) — don't build it for LF; hide that setup section
  for PERCENT_TIPOUT venues.
- Timecards: same `SearchTimecards` flow, LF token, jobs → roles mapping
  screen seeded from Square job names.

## 5. UI deltas

- Venue picker at launch; venue name always visible; switching returns
  to the picker (no cross-venue back-stack).
- Daily Review for PERCENT_TIPOUT: per-server tip rows (card + declared
  cash, provenance badges), unattributed-tips bucket, pool summary
  (busser/host/BOH with recipients and shares), empty-pool flags, same
  sticky finalize bar and confirmation-summary pattern as the TL redesign.
- Payroll export: same CSV shape, venue-stamped; per-employee lines show
  keep vs. pool-share vs. gratuity components so CBS can trace any number.

## 6. Owner decisions to append to CLAUDE.md log

| Decision | Ruling |
|---|---|
| Venue model | TL+NT = one venue. La Fontana = separate venue, separate Square merchant, PERCENT_TIPOUT model. |
| LF percentages | Server keeps 65%; 20% bussers, 10% host, 5% BOH — of each server's OWN tips. Configurable with effective dates; must sum to 100%. |
| Pool splits | **EVEN SPLIT** among role members who worked that day (busser, host, BOH pools). Hours-proportional toggle exists but ships OFF. |
| No host worked | Host share re-splits **75% to the contributing server / 20% to bussers / 5% to BOH** (effective: 72.5/22/5.5). Day flagged. |
| No bussers / no BOH | Pool returns pro-rata to contributing servers; day flagged. No re-split defined — do not invent. |
| Granularity | One shift per day at LF → per business day in v1. Per-shift pooling reserved for a future revision. |
| Roles | **Fixed per person** (servers always servers, etc.), set on the employee mapping screen. Per-job/per-day roles out of v1 scope; mismatch = warning, assigned role wins. |
| LF cash tips | Declaration policy imminent; pipeline built now, zeros until staff start declaring. |
| RBAC | Deferred; schema (`user_venue_access`) added now, unenforced. |

## 7. Tests & rollout

- New golden-style synthetic fixtures for PERCENT_TIPOUT: normal day
  (1 host, 2 bussers, 3 servers, 2 BOH — verify even splits); no-host day
  (verify 75/20/5 re-split conserves to the cent); no-busser day (pro-rata
  return); no-host AND no-busser day (cascade order); unattributed tips;
  zero-tip server; single-busser day; rounding-residual cases across even
  splits; declared-cash day; role-mismatch warning. Conservation asserted
  on every fixture.
- Regression: full TL suite green, byte-identical TL exports for an
  already-finalized period re-generated before/after the refactor.
- Rollout: LF goes through its own parallel run against the current
  manual method for one pay period before it becomes system of record —
  same discipline as TL, no exceptions.

## 8. Out of scope (do not build)

Cross-venue dashboards/consolidation; RBAC enforcement; per-shift pooling;
tippable-window clipping at LF; La Fontana historical import; hosting.
