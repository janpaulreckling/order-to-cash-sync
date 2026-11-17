import xmlrpc.client
from typing import Any

import pytest

from o2c_sync.connectors.odoo import OdooERP
from o2c_sync.errors import PermanentError, TransientError
from o2c_sync.mapping import FieldMapper
from o2c_sync.retry import retry_policy
from tests.conftest import opportunity


class FakeOdoo:
    """Answers execute_kw like Odoo would, and records every call."""

    def __init__(self, products: dict[str, int], currency: str = "USD") -> None:
        self.products, self.currency = products, currency
        self.calls: list[tuple[str, str, list[Any], dict[str, Any]]] = []
        self.fail_next: list[Exception] = []
        self.last_lines: list[dict[str, Any]] = []

    def authenticate(self, *_: Any) -> int:
        return 2

    def execute_kw(self, _db: str, _uid: int, _pw: str, model: str, method: str,
                   args: list[Any], kw: dict[str, Any]) -> Any:
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.calls.append((model, method, args, kw))
        if (model, method) == ("res.company", "search_read"):
            return [{"currency_id": [1, self.currency]}]
        if (model, method) == ("product.product", "search_read"):
            wanted = args[0][0][2]
            return [{"id": i, "default_code": c} for c, i in self.products.items() if c in wanted]
        if method == "search":
            return []
        if method == "create":
            if model == "sale.order":
                self.last_lines = [line[2] for line in args[0]["order_line"]]
            return 42
        if (model, method) == ("sale.order", "read"):
            # Like Odoo: unit prices are stored with 2 decimals (Product Price precision).
            total = sum(round(v["price_unit"], 2) * v["product_uom_qty"]
                        * (1 - v["discount"] / 100) for v in self.last_lines)
            return [{"amount_total": round(total, 2)}]
        if (model, method) == ("sale.order", "search_read"):
            return []
        raise AssertionError(f"unexpected call {model}.{method}")


@pytest.fixture
def odoo() -> tuple[OdooERP, FakeOdoo]:
    fake = FakeOdoo({"TENT-2P": 7, "PACK-45L": 8})
    erp = OdooERP("http://odoo.test", "db", "admin", "pw")
    erp._common = erp._models = fake  # type: ignore[assignment]
    erp._retry = retry_policy(3, max_wait=0)
    return erp, fake


def test_create_order_sends_lines_with_product_ids(odoo, mapper: FieldMapper) -> None:
    erp, fake = odoo
    ref = erp.create_order(mapper.to_sales_order(opportunity()), customer_ref="5")

    assert ref == "42"
    [(_, _, [values], _)] = [c for c in fake.calls if c[:2] == ("sale.order", "create")]
    assert values["partner_id"] == 5
    assert values["client_order_ref"] == "006Hs00001AbCdEIAA"
    assert [(line[2]["product_id"], line[2]["product_uom_qty"]) for line in values["order_line"]] \
        == [(8, 1.0), (7, 2.0)]  # canonical (SKU) order


def test_unknown_sku_is_permanent(odoo, mapper: FieldMapper) -> None:
    erp, _ = odoo
    order = mapper.to_sales_order(opportunity(lines=[("KAYAK-X", 1, 900.0)]))
    with pytest.raises(PermanentError, match="KAYAK-X"):
        erp.create_order(order, "5")


def test_erp_total_is_reconciled_after_write(odoo, mapper: FieldMapper) -> None:
    # Seen live: after a quantity edit over the API, Salesforce keeps the line
    # total and stores a sub-cent unit price; Odoo rounds it to the cent.
    erp, _ = odoo
    order = mapper.to_sales_order(opportunity(lines=[("PACK-45L", 21, 177.52380952)],
                                              amount=3728.00))
    with pytest.raises(PermanentError, match="ERP total 3727.92 differs from CRM total 3728.00"):
        erp.create_order(order, "5")


def test_currency_mismatch_is_rejected(odoo, mapper: FieldMapper) -> None:
    erp, fake = odoo
    fake.currency = "EUR"
    with pytest.raises(PermanentError, match="FX"):
        erp.create_order(mapper.to_sales_order(opportunity()), "5")


def test_server_errors_are_retried(odoo) -> None:
    erp, fake = odoo
    fake.fail_next = [
        xmlrpc.client.ProtocolError("u", 503, "Service Unavailable", {}),
        ConnectionRefusedError("down"),
    ]
    assert erp.find_order("006Hs00001AbCdEIAA") is None  # third attempt succeeds


def test_retries_are_bounded(odoo) -> None:
    erp, fake = odoo
    fake.fail_next = [ConnectionRefusedError("down")] * 5
    with pytest.raises(TransientError):
        erp.find_order("006Hs00001AbCdEIAA")


def test_validation_fault_is_permanent_and_not_retried(odoo) -> None:
    erp, fake = odoo
    fake.fail_next = [xmlrpc.client.Fault(2, "Traceback...\nValidationError: bad partner")]
    with pytest.raises(PermanentError, match="bad partner"):
        erp.find_order("x")
    assert fake.fail_next == []
