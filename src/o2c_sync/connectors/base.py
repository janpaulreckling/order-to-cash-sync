"""Connector contracts. The engine depends on these protocols, never on a vendor SDK."""

from collections.abc import Iterator
from datetime import datetime
from typing import Any, Protocol

from o2c_sync.models import Customer, ErpOrder, ErpOrderStatus, SalesOrder


class CRMSource(Protocol):
    def fetch_closed_won(self, since: datetime | None) -> Iterator[dict[str, Any]]:
        """Won opportunities modified at/after `since`, oldest first."""
        ...

    def update_order_status(self, status: ErpOrderStatus) -> None:
        """Write ERP financial status back onto the CRM record."""
        ...


class ERPTarget(Protocol):
    def upsert_customer(self, customer: Customer) -> str: ...

    def find_order(self, external_id: str) -> ErpOrder | None: ...

    def create_order(self, order: SalesOrder, customer_ref: str) -> str: ...

    def update_order(self, ref: str, order: SalesOrder, customer_ref: str) -> None: ...

    def list_status_changes(self, since: datetime | None) -> Iterator[ErpOrderStatus]:
        """Orders whose invoicing/payment status changed at/after `since`, oldest first."""
        ...
