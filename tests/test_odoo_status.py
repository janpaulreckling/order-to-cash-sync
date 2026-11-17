from decimal import Decimal

from o2c_sync.connectors.odoo import derive_invoice_status
from o2c_sync.models import InvoiceStatus


def inv(state: str, total: float, residual: float) -> dict[str, object]:
    return {"state": state, "amount_total": total, "amount_residual": residual}


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
