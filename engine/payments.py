"""Credit-tip summation with refund handling (CLAUDE.md §3.2).

Pure function over payment records so the refund logic is testable in M1;
the Square sync layer (M3) will adapt real Payments API responses into
`Payment` records.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

CARD = "CARD"
COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class Payment:
    tender_type: str  # CARD, CASH, ...
    status: str  # COMPLETED, FAILED, CANCELED, ...
    tip_cents: int = 0
    refunded_tip_cents: int = 0


def net_credit_tip_cents(payments: Iterable[Payment]) -> int:
    """Sum tip_money on COMPLETED card payments, net of refunded tips.
    Cash-tender tips are excluded (cash tips come from timecard declarations)."""
    total = 0
    for p in payments:
        if p.tender_type != CARD or p.status != COMPLETED:
            continue
        total += p.tip_cents - p.refunded_tip_cents
    return total
