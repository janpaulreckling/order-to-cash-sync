from decimal import Decimal

from o2c_sync.connectors.odoo import derive_invoice_status
from o2c_sync.models import InvoiceStatus


def inv(state: str, total: float, residual: float,
        move_type: str = "out_invoice") -> dict[str, object]:
    return {"state": state, "amount_total": total, "amount_residual": residual,
            "move_type": move_type}


def refund(total: float, residual: float) -> dict[str, object]:
    return inv("posted", total, residual, move_type="out_refund")


def test_no_posted_invoice_means_not_invoiced() -> None:
    assert derive_invoice_status([inv("draft", 100, 100)]) == (
        InvoiceStatus.NOT_INVOICED, Decimal("0.00"))


def test_open_invoice() -> None:
    assert derive_invoice_status([inv("posted", 887, 887)]) == (
        InvoiceStatus.INVOICED, Decimal("887.00"))


def test_partial_payment() -> None:
    assert derive_invoice_status([inv("posted", 887, 387)]) == (
        InvoiceStatus.PARTIALLY_PAID, Decimal("387.00"))


def test_paid_ignores_cancelled_invoices() -> None:
    assert derive_invoice_status([inv("posted", 887, 0), inv("cancel", 50, 50)]) == (
        InvoiceStatus.PAID, Decimal("0.00"))


def test_reversed_invoice_is_credited_not_paid() -> None:
    # The credit note is reconciled with the invoice: both residuals are 0.
    assert derive_invoice_status([inv("posted", 887, 0), refund(887, 0)]) == (
        InvoiceStatus.CREDITED, Decimal("0.00"))


def test_partial_credit_note_reduces_amount_due_without_counting_as_cash() -> None:
    # $87 credited for damaged goods, nothing paid yet.
    assert derive_invoice_status([inv("posted", 887, 800), refund(87, 0)]) == (
        InvoiceStatus.INVOICED, Decimal("800.00"))


def test_credit_note_plus_payment_of_the_rest_is_paid() -> None:
    assert derive_invoice_status([inv("posted", 887, 0), refund(87, 0)]) == (
        InvoiceStatus.PAID, Decimal("0.00"))


def test_unreconciled_credit_note_is_netted() -> None:
    assert derive_invoice_status([inv("posted", 887, 887), refund(87, 87)]) == (
        InvoiceStatus.INVOICED, Decimal("800.00"))
