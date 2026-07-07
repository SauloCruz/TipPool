"""Refunded-payment tip handling (§3.2 / §5)."""

from engine import Payment, net_credit_tip_cents


def test_card_completed_tips_summed():
    payments = [
        Payment("CARD", "COMPLETED", tip_cents=500),
        Payment("CARD", "COMPLETED", tip_cents=725),
    ]
    assert net_credit_tip_cents(payments) == 1225


def test_cash_tender_tips_excluded():
    payments = [
        Payment("CARD", "COMPLETED", tip_cents=500),
        Payment("CASH", "COMPLETED", tip_cents=300),
    ]
    assert net_credit_tip_cents(payments) == 500


def test_refunded_tips_subtracted():
    payments = [
        Payment("CARD", "COMPLETED", tip_cents=500, refunded_tip_cents=500),  # full
        Payment("CARD", "COMPLETED", tip_cents=800, refunded_tip_cents=300),  # partial
    ]
    assert net_credit_tip_cents(payments) == 500


def test_non_completed_payments_excluded():
    payments = [
        Payment("CARD", "COMPLETED", tip_cents=400),
        Payment("CARD", "FAILED", tip_cents=999),
        Payment("CARD", "CANCELED", tip_cents=999),
    ]
    assert net_credit_tip_cents(payments) == 400


def test_empty_day():
    assert net_credit_tip_cents([]) == 0
