# Golden Fixtures — Tavern Law Tip Pool Engine

Real historical data extracted from `Payroll_Tip_Pool_-_2025.xlsx` for validating
the Milestone 1 calculation engine. **46 days across 3 pay periods:**

| Fixture | Period | Coverage |
|---|---|---|
| `6.30.26` | Jun 16–30, 2026 (15 days) | Clean baseline — no events |
| `6.15.26` | Jun 1–15, 2026 (15 days) | Event food sales + event tips + high auto-gratuity |
| `5.31.26` | May 16–31, 2026 (16 days) | 16-day period + events — tests the full-width column case |

## Schema (per day)
```jsonc
{
  "date": "2026-06-16",
  "inputs": {
    "food_sales": 530.0,          // non-alcohol gross
    "event_food_sales": 0.0,
    "credit_tips": 622.47,
    "cash_tips": 0.0,
    "event_tips": 0.0,
    "auto_gratuity": 0.0,         // service charges — SEPARATE pool
    "boh_worked": ["Staff-01", "Staff-08", "Staff-13"],
    "foh_hours": {"Staff-02": 7.0, "Staff-04": 7.0, ...}   // already-tippable hours
  },
  "expected": {
    "boh_allocation": 26.50,      // 0.05*food + 0.10*event_food
    "total_tips": 622.47,
    "foh_pool": 595.97,           // total_tips - boh_allocation
    "auto_gratuity": 0.0,
    "foh_payouts": { ... },       // hours-proportional, cents-exact
    "gratuity_payouts": { ... },  // hours-proportional, cents-exact, separate pool
    "boh_payouts": { ... }        // even split among boh_worked
  },
  "flags": { "negative_foh_pool": false },
  "spreadsheet_deviations": []    // empty = sheet and correct calc agree
}
```

## Important notes for the test suite

1. **Expected values are the CORRECT calculation, not bug-for-bug spreadsheet
   parity.** Payouts were recomputed in integer cents with **largest-remainder
   rounding** (residual cents go to the largest fractional remainder, tie-break
   by hours desc, then name). Conservation invariants hold exactly on every day:
   - Σ foh_payouts == foh_pool
   - Σ boh_payouts == boh_allocation
   - Σ gratuity_payouts == auto_gratuity

2. **Zero deviations found vs. the spreadsheet** across all 46 days (± $0.02
   per employee/day). The two known Excel bugs (entered-headcount divisor,
   B:P vs B:Q summation) never triggered in these periods — managers kept
   counts consistent and day-16 columns were unused where it mattered. Engine
   output should therefore match legacy payroll history as well.

3. **`foh_hours` are already tippable-clipped** (the spreadsheet users entered
   window-clipped hours manually). These fixtures test the *distribution* engine,
   not the timecard-clipping layer. Write separate unit tests for §2a clipping
   using synthetic timecards.

4. Employee keys are pseudonymized (Staff-NN, order-preserving so the
   alphabetical residual-cent tie-break is unchanged); the app keys on
   Square `team_member_id`
   with names as display labels.

## Suggested pytest skeleton
```python
import json, pytest
from engine import compute_day   # your M1 module

FIXTURES = json.load(open("golden_fixtures.json"))
DAYS = [(f["source_tab"], d) for f in FIXTURES for d in f["days"]]

@pytest.mark.parametrize("tab,day", DAYS, ids=lambda x: x if isinstance(x, str) else x["date"])
def test_golden_day(tab, day):
    out = compute_day(**day["inputs"])
    exp = day["expected"]
    assert out.boh_allocation == pytest.approx(exp["boh_allocation"], abs=0.005)
    assert out.foh_pool       == pytest.approx(exp["foh_pool"], abs=0.005)
    for name, amt in exp["foh_payouts"].items():
        assert out.foh_payouts[name] == pytest.approx(amt, abs=0.01)
    for name, amt in exp["gratuity_payouts"].items():
        assert out.gratuity_payouts[name] == pytest.approx(amt, abs=0.01)
    for name, amt in exp["boh_payouts"].items():
        assert out.boh_payouts[name] == pytest.approx(amt, abs=0.01)
    # conservation
    assert sum(out.foh_payouts.values())      == pytest.approx(out.foh_pool, abs=0.005)
    assert sum(out.boh_payouts.values())      == pytest.approx(out.boh_allocation, abs=0.005)
    assert sum(out.gratuity_payouts.values()) == pytest.approx(day["inputs"]["auto_gratuity"], abs=0.005)
```

## Data-quality note (excluded periods)
Tabs `2.28.26`, `2.15.26`, and `1.31.26` contain anomalous event-tip entries
(one ~$13.4k event-tips total; two negative totals) — likely entry errors worth
reviewing with the manager, but unusable as golden data. Tabs before Feb 2026
have no cached formula values (inputs intact; outputs unverifiable without
recalculation) — the historical importer can still load their inputs.
