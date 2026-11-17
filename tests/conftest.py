from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from o2c_sync.engine import SyncEngine
from o2c_sync.errors import PermanentError, RateLimitExceeded
from o2c_sync.mapping import FieldMapper
from o2c_sync.models import Customer, ErpOrder, ErpOrderStatus, SalesOrder
from o2c_sync.state import StateStore

MAPPING = Path(__file__).resolve().parents[1] / "config" / "field_mapping.yaml"


def opportunity(
    opp_id: str = "006Hs00001AbCdEIAA",
    *,
    modified: str = "2026-11-02T14:03:11.000+0000",
    lines: list[tuple[str, float, float]] | None = None,
    amount: float | None = None,
    account_id: str = "001Hs00001XyZaBIAV",
    country: str = "United States",
) -> dict[str, Any]:
    """A record shaped exactly like the Salesforce REST response."""
    lines = lines if lines is not None else [("TENT-2P", 2, 349.0), ("PACK-45L", 1, 189.0)]
    line_records = [
        {
            "attributes": {"type": "OpportunityLineItem"},
            "Quantity": qty,
            "UnitPrice": price,
            "Discount": None,
            "Product2": {"attributes": {"type": "Product2"}, "ProductCode": sku, "Name": sku},
        }
        for sku, qty, price in lines
    ]
    return {
        "attributes": {"type": "Opportunity"},
        "Id": opp_id,
        "Name": f"Deal {opp_id[-4:]}",
        "Amount": amount if amount is not None else sum(q * p for _, q, p in lines),
        "CloseDate": "2026-11-01",
        "LastModifiedDate": modified,
        "Account": {
            "attributes": {"type": "Account"},
            "Id": account_id,
            "Name": "Summit Gear Co",
            "Phone": "(212) 555-0100",
            "BillingStreet": "55 Lexington Ave",
            "BillingCity": "New York",
            "BillingState": "NY",
            "BillingPostalCode": "10010",
            "BillingCountry": country,
        },
        "OpportunityLineItems": {"totalSize": len(line_records), "records": line_records},
    }


class FakeCRM:
    def __init__(self, records: list[dict[str, Any]], rate_limit_after: int | None = None):
        self.records = records
        self.rate_limit_after = rate_limit_after
        self.status_updates: list[ErpOrderStatus] = []

    def fetch_closed_won(self, since: datetime | None) -> Iterator[dict[str, Any]]:
        mapper = FieldMapper.from_yaml(MAPPING)
        rows = sorted(self.records, key=lambda r: r["LastModifiedDate"])
        for i, row in enumerate(rows):
            if self.rate_limit_after is not None and i >= self.rate_limit_after:
                raise RateLimitExceeded("daily limit")
            modified = mapper.source_modified(row)
            if since is None or modified is None or modified >= since:
                yield row

    def update_order_status(self, status: ErpOrderStatus) -> None:
        self.status_updates.append(status)


class FakeERP:
    def __init__(self, skus: set[str] | None = None) -> None:
        self.skus = skus if skus is not None else {"TENT-2P", "PACK-45L", "STOVE-1"}
        self.customers: dict[str, Customer] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.statuses: list[ErpOrderStatus] = []
        self.writes = 0

    def upsert_customer(self, customer: Customer) -> str:
        self.customers[customer.external_id] = customer
        return f"P-{customer.external_id[-4:]}"

    def _check(self, order: SalesOrder) -> None:
        missing = {line.sku for line in order.lines} - self.skus
        if missing:
            raise PermanentError(f"unknown SKU(s) in ERP product master: {sorted(missing)}")

    def find_order(self, external_id: str) -> ErpOrder | None:
        o = self.orders.get(external_id)
        if o is None:
            return None
        return ErpOrder(ref=o["ref"], name=o["ref"], state=o["state"],
                        editable=o["state"] in {"draft", "sent"})

    def create_order(self, order: SalesOrder, customer_ref: str) -> str:
        self._check(order)
        ref = f"S{len(self.orders) + 1:05d}"
        self.orders[order.external_id] = {"ref": ref, "state": "draft", "order": order}
        self.writes += 1
        return ref

    def update_order(self, ref: str, order: SalesOrder, customer_ref: str) -> None:
        self._check(order)
        self.orders[order.external_id]["order"] = order
        self.writes += 1

    def list_status_changes(self, since: datetime | None) -> Iterator[ErpOrderStatus]:
        yield from (s for s in self.statuses if since is None or s.updated_at >= since)


@pytest.fixture
def mapper() -> FieldMapper:
    return FieldMapper.from_yaml(MAPPING)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(f"sqlite:///{tmp_path / 'state.db'}")


@pytest.fixture
def make_engine(mapper: FieldMapper, store: StateStore):  # type: ignore[no-untyped-def]
    def _make(crm: FakeCRM, erp: FakeERP | None = None) -> tuple[SyncEngine, FakeERP]:
        erp = erp or FakeERP()
        return SyncEngine(crm, erp, mapper, store), erp

    return _make
