"""Canonical domain model.

Connectors translate *into* and *out of* these types. The engine only ever sees
canonical objects, which is what makes the ERP side swappable (Odoo today,
NetSuite SuiteTalk tomorrow).
"""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

CENT = Decimal("0.01")
TOTAL_TOLERANCE = CENT


class _Canonical(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class Address(_Canonical):
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country_code: str = Field(default="US", pattern=r"^[A-Z]{2}$")


class Customer(_Canonical):
    external_id: str = Field(min_length=15, max_length=18)  # Salesforce Account Id
    name: str = Field(min_length=1)
    phone: str | None = None
    billing_address: Address


class OrderLine(_Canonical):
    sku: str = Field(min_length=1)
    description: str | None = None
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    discount_pct: Decimal = Field(default=Decimal(0), ge=0, le=100)

    @property
    def net_amount(self) -> Decimal:
        gross = self.quantity * self.unit_price
        return (gross * (1 - self.discount_pct / 100)).quantize(CENT)


class SalesOrder(_Canonical):
    external_id: str = Field(min_length=15, max_length=18)  # Salesforce Opportunity Id
    name: str
    customer: Customer
    close_date: date
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    lines: tuple[OrderLine, ...] = Field(min_length=1)
    expected_total: Decimal | None = None
    source_last_modified: datetime

    @property
    def total(self) -> Decimal:
        return sum((line.net_amount for line in self.lines), Decimal("0.00"))

    @model_validator(mode="after")
    def _lines_must_match_crm_amount(self) -> "SalesOrder":
        # Business rule: never book an order whose line items disagree with the
        # amount Sales closed on. That is a revenue-recognition problem waiting
        # to happen, so a human has to look at it.
        if self.expected_total is not None and (
            abs(self.total - self.expected_total) > TOTAL_TOLERANCE
        ):
            raise ValueError(
                f"line items sum to {self.total} but CRM amount is {self.expected_total}"
            )
        return self

    def fingerprint(self) -> str:
        """Content hash used for idempotency. Timestamps are excluded on purpose:
        a record touched in Salesforce without a business-relevant change must
        not trigger an ERP write."""
        payload = self.model_dump_json(exclude={"source_last_modified"})
        return hashlib.sha256(payload.encode()).hexdigest()


class InvoiceStatus(StrEnum):
    NOT_INVOICED = "Not Invoiced"
    INVOICED = "Invoiced"
    PARTIALLY_PAID = "Partially Paid"
    PAID = "Paid"
    CREDITED = "Credited"  # fully reversed by credit notes; no cash received


class ErpOrderStatus(_Canonical):
    """Financial status of an order, flowing back from ERP to CRM."""

    external_id: str
    erp_order_ref: str
    invoice_status: InvoiceStatus
    amount_due: Decimal
    updated_at: datetime

    def fingerprint(self) -> str:
        payload = self.model_dump_json(exclude={"updated_at"})
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class ErpOrder:
    """Minimal view of an order as it currently exists in the ERP."""

    ref: str
    name: str
    state: str
    editable: bool
