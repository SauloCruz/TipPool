"""Golden-file tests: all 46 historical days from Payroll_Tip_Pool_-_2025.xlsx."""

import json
from pathlib import Path

import pytest

from engine import compute_day

FIXTURE_PATH = Path(__file__).parent / "Fixtures" / "golden_fixtures.json"
FIXTURES = json.loads(FIXTURE_PATH.read_text())
DAYS = [(f["source_tab"], d) for f in FIXTURES for d in f["days"]]


def test_fixture_coverage():
    assert len(FIXTURES) == 3
    assert len(DAYS) == 46


@pytest.mark.parametrize(
    "tab,day", DAYS, ids=[f"{tab}:{d['date']}" for tab, d in DAYS]
)
def test_golden_day(tab, day):
    out = compute_day(**day["inputs"])
    exp = day["expected"]

    assert out.total_tips == pytest.approx(exp["total_tips"], abs=0.005)
    assert out.boh_allocation == pytest.approx(exp["boh_allocation"], abs=0.005)
    assert out.foh_pool == pytest.approx(exp["foh_pool"], abs=0.005)
    assert out.auto_gratuity == pytest.approx(exp["auto_gratuity"], abs=0.005)

    assert set(out.foh_payouts) == set(exp["foh_payouts"])
    for name, amt in exp["foh_payouts"].items():
        assert out.foh_payouts[name] == pytest.approx(amt, abs=0.01), name

    assert set(out.gratuity_payouts) == set(exp["gratuity_payouts"])
    for name, amt in exp["gratuity_payouts"].items():
        assert out.gratuity_payouts[name] == pytest.approx(amt, abs=0.01), name

    assert set(out.boh_payouts) == set(exp["boh_payouts"])
    for name, amt in exp["boh_payouts"].items():
        assert out.boh_payouts[name] == pytest.approx(amt, abs=0.01), name

    # conservation — exact in cents, not just approx
    assert sum(out.foh_payout_cents.values()) == out.foh_pool_cents
    assert sum(out.boh_payout_cents.values()) == out.boh_allocation_cents
    assert sum(out.gratuity_payout_cents.values()) == out.auto_gratuity_cents

    assert out.flags["negative_foh_pool"] == day["flags"]["negative_foh_pool"]


@pytest.mark.parametrize(
    "tab,day", DAYS, ids=[f"{tab}:{d['date']}" for tab, d in DAYS]
)
def test_golden_day_cents_exact(tab, day):
    """Fixture payouts were generated cents-exact; match them exactly, not
    just within a cent, to lock in the largest-remainder tie-break rules."""
    out = compute_day(**day["inputs"])
    exp = day["expected"]
    for name, amt in exp["foh_payouts"].items():
        assert out.foh_payout_cents[name] == round(amt * 100), f"foh:{name}"
    for name, amt in exp["gratuity_payouts"].items():
        assert out.gratuity_payout_cents[name] == round(amt * 100), f"grat:{name}"
    for name, amt in exp["boh_payouts"].items():
        assert out.boh_payout_cents[name] == round(amt * 100), f"boh:{name}"
