# Claude Design Brief — Tip Pool App UI (Tavern Law)

> Paste this as the opening prompt in a Claude Design project, after linking the
> app's code repository (design-system import). One screen per session to manage
> token usage. Priority order: Daily Review → Period Dashboard → Setup/Mappings.

---

## Product context

Internal payroll tool for a Seattle cocktail bar. Calculates the nightly tip pool:
Square auto-pulls sales, credit tips, declared cash tips, auto-gratuity, and
timecards; a manager reviews, enters 2–3 manual values, and finalizes. The app is
live in production with real daily data. Backend and calculation logic are done and
correct — **this is a visual/UX redesign only. Do not change any workflow steps,
fields, data, or business logic.**

Current stack: no-build vanilla-JS SPA, dark theme, mobile-first. The design system
imported from the repo is the starting point — evolve it, don't replace it wholesale.

## The user and the moment (design for this, ruthlessly)

A bar manager, standing, at **12:30–2:00 AM after a full shift**, on a phone,
possibly in a dim room, thumb-only. Tired. Wants to be done in under 3 minutes.
Secondary user: the owner, reviewing on a laptop occasionally — desktop is a
nice-to-have, phone is the product.

Design implications (hard requirements):
- One-handed, one-thumb operation; primary actions in thumb reach (bottom half)
- Touch targets ≥ 44px; generous spacing between destructive/primary actions
- Dark theme stays (dim environments); check contrast at low brightness
- Minimal typing: steppers, chips, and confirm-taps over keyboards wherever possible
- Reading order = task order: confirm pulled data → enter manual values → scan
  payouts → finalize. No hunting.

## Screen 1 (this session): Daily Review

What's on it today, in order:
1. Date + day status (draft / finalized / flagged)
2. Auto-pulled values, each with a **provenance badge** (Square / manual /
   overridden — overridden shows the original Square value) and an override flow
3. Warning banners in plain English (e.g., all-zero cash declarations, unmapped
   category blocking, missing clock-outs) — some warnings are mutable per-warning
4. Manual inputs: event food sales, event tips (default 0; most days untouched)
5. FOH hours list (exact-minute values, e.g., 6.85) with per-person adjust;
   kitchen roster as checkboxes
6. Live computed distribution: BOH allocation, FOH pool, per-person payout table
7. Sticky bottom bar: running total + **Finalize** button

### Problems to solve (observed, not hypothetical)
- The screen is long; managers scroll past confirmed-good data to reach the two
  fields they actually touch. Explore progressive disclosure: collapsed "all good"
  sections that expand only on tap or when a warning applies.
- Provenance badges + warnings + numbers compete visually; hierarchy is flat.
  A tired reader should see in one glance: what needs my attention vs. what's fine.
- Finalize is consequential (writes an immutable snapshot). It should feel
  deliberate — but not add friction on clean days. Explore a confirm pattern that
  summarizes what's being locked (totals + any overrides + any muted warnings).
- The payout table is dense on a phone. Explore: per-person rows optimized for
  scanning ("is anyone's number obviously wrong?") rather than spreadsheet fidelity.

### Explore 3 directions (distinct, not restyles of each other)
- **A — Checklist flow:** the day as a vertical checklist; sections auto-collapse
  when their data is clean, expand when flagged. Finalize unlocks when all sections
  are confirmed. Prioritizes speed on clean days.
- **B — Review-first dashboard:** a top summary card (pool totals, warning count,
  "2 items need you") that deep-links into just the sections needing attention.
  Prioritizes triage.
- **C — Stepper/wizard:** confirm → enter → review → finalize as discrete steps
  with progress. Prioritizes error-prevention for new managers; test whether it's
  too slow for veterans.

### What NOT to do
- No light theme, no removing provenance badges or warnings (compliance/audit
  features), no combining tips and auto-gratuity anywhere (they're legally
  distinct income lines), no cutting the original-Square-value display on overrides
- No new fields, no reordering the underlying calculation, no navigation redesign
  in this session
- Keep it a no-build vanilla-JS-compatible design: standard HTML/CSS patterns,
  no component-library dependencies the repo doesn't have

## Success criteria
- Clean-day finalize (no warnings, no manual events): **≤ 60 seconds, ≤ 8 taps**
- A flagged item is visually unmissable within 2 seconds of screen load
- Owner can read a finalized day on desktop without horizontal scrolling
- Managers prefer it over the current screen in a 5-minute pre-shift hallway test

## Handoff
Winner goes back to Claude Code for implementation against the existing repo
(two-way sync). Deliver as HTML/CSS consistent with the imported design system;
note any new design tokens introduced so Claude Code updates the system in one place.
