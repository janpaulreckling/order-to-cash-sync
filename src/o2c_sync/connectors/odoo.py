"""Odoo 17 ERP connector (XML-RPC external API).

Odoo stands in for NetSuite: same concepts (customer master, product master,
sales order lifecycle, invoices), and it runs locally for free. A NetSuite
connector would implement the same ERPTarget protocol against SuiteTalk REST.

Mapping of identities:
    res.partner.ref               <- Salesforce Account Id
    sale.order.client_order_ref   <- Salesforce Opportunity Id
    product.product.default_code  <- Salesforce Product2.ProductCode (SKU)
"""

import socket
import xmlrpc.client
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from o2c_sync.errors import PermanentError, TransientError
from o2c_sync.models import (
    CENT,
    Customer,
    ErpOrder,
    ErpOrderStatus,
    InvoiceStatus,
    SalesOrder,
)
from o2c_sync.retry import retry_policy

EDITABLE_STATES = frozenset({"draft", "sent"})  # quotation stages; "sale" = confirmed
ODOO_DT = "%Y-%m-%d %H:%M:%S"


def _odoo_dt(value: datetime) -> str:
    return value.astimezone(UTC).strftime(ODOO_DT)


def _parse_odoo_dt(value: str) -> datetime:
    return datetime.strptime(value, ODOO_DT).replace(tzinfo=UTC)


def derive_invoice_status(invoices: Iterable[dict[str, Any]]) -> tuple[InvoiceStatus, Decimal]:
    """Collapse an order's customer invoices into one CRM-facing status.

    Only posted invoices count; drafts are not receivables yet.
    """
    posted = [inv for inv in invoices if inv.get("state") == "posted"]
    if not posted:
        return InvoiceStatus.NOT_INVOICED, Decimal("0.00")

    due = sum((Decimal(str(inv["amount_residual"])) for inv in posted), Decimal(0)).quantize(CENT)
    total = sum((Decimal(str(inv["amount_total"])) for inv in posted), Decimal(0)).quantize(CENT)
    if due <= 0:
        return InvoiceStatus.PAID, Decimal("0.00")
    if due < total:
        return InvoiceStatus.PARTIALLY_PAID, due
    return InvoiceStatus.INVOICED, due


class OdooERP:
    def __init__(
        self, url: str, db: str, username: str, password: str, max_retries: int = 5
    ) -> None:
        self._db, self._username, self._password = db, username, password
        self._common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        self._models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
        self._uid: int | None = None
        self._retry = retry_policy(max_retries)
        self._country_cache: dict[str, int | bool] = {}
        self._company_currency: str | None = None

    # -------------------------------------------------------------- rpc
    def _execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        def attempt() -> Any:
            try:
                if self._uid is None:
                    uid: Any = self._common.authenticate(
                        self._db, self._username, self._password, {}
                    )
                    if not uid:
                        raise PermanentError("Odoo authentication failed; check credentials")
                    self._uid = int(uid)
                return self._models.execute_kw(
                    self._db, self._uid, self._password, model, method, list(args), kwargs
                )
            except (ConnectionError, TimeoutError, socket.gaierror) as exc:
                raise TransientError(f"Odoo unreachable: {exc}") from exc
            except xmlrpc.client.ProtocolError as exc:
                if exc.errcode >= 500 or exc.errcode == 429:
                    raise TransientError(f"Odoo HTTP {exc.errcode}") from exc
                raise PermanentError(f"Odoo HTTP {exc.errcode}: {exc.errmsg}") from exc
            except xmlrpc.client.Fault as exc:
                # Server-side validation errors; the last line is the readable one.
                raise PermanentError(f"Odoo {model}.{method}: "
                                     f"{exc.faultString.strip().splitlines()[-1]}") from exc

        return self._retry(attempt)

    # ---------------------------------------------------------- lookups
    def _country_id(self, code: str) -> int | bool:
        if code not in self._country_cache:
            ids = self._execute("res.country", "search", [("code", "=", code)], limit=1)
            self._country_cache[code] = ids[0] if ids else False
        return self._country_cache[code]

    def _currency(self) -> str:
        if self._company_currency is None:
            [company] = self._execute("res.company", "search_read", [], fields=["currency_id"],
                                      limit=1)
            self._company_currency = company["currency_id"][1]
        return self._company_currency

    def _product_ids(self, skus: Sequence[str]) -> dict[str, int]:
        rows = self._execute("product.product", "search_read",
                             [("default_code", "in", list(set(skus)))],
                             fields=["id", "default_code"])
        found = {row["default_code"]: row["id"] for row in rows}
        missing = sorted(set(skus) - found.keys())
        if missing:
            # Business rule: the ERP owns the product master and pricing.
            # The sync never invents products.
            raise PermanentError(f"unknown SKU(s) in ERP product master: {', '.join(missing)}")
        return found

    def _order_lines(self, order: SalesOrder) -> list[tuple[int, int, dict[str, Any]]]:
        if order.currency != self._currency():
            raise PermanentError(
                f"order currency {order.currency} differs from ERP company currency "
                f"{self._currency()}; FX orders are out of scope"
            )
        products = self._product_ids([line.sku for line in order.lines])
        return [
            (0, 0, {
                "product_id": products[line.sku],
                "name": line.description or line.sku,
                "product_uom_qty": float(line.quantity),
                "price_unit": float(line.unit_price),
                "discount": float(line.discount_pct),
            })
            for line in order.lines
        ]

    # --------------------------------------------------------- protocol
    def upsert_customer(self, customer: Customer) -> str:
        addr = customer.billing_address
        values = {
            "name": customer.name,
            "ref": customer.external_id,
            "is_company": True,
            "phone": customer.phone or False,
            "street": addr.street or False,
            "city": addr.city or False,
            "zip": addr.postal_code or False,
            "country_id": self._country_id(addr.country_code),
        }
        ids = self._execute("res.partner", "search", [("ref", "=", customer.external_id)],
                            limit=1)
        if ids:
            self._execute("res.partner", "write", ids, values)
            return str(ids[0])
        return str(self._execute("res.partner", "create", values))

    def find_order(self, external_id: str) -> ErpOrder | None:
        rows = self._execute("sale.order", "search_read",
                             [("client_order_ref", "=", external_id)],
                             fields=["id", "name", "state"], limit=1)
        if not rows:
            return None
        row = rows[0]
        return ErpOrder(ref=str(row["id"]), name=row["name"], state=row["state"],
                        editable=row["state"] in EDITABLE_STATES)

    def create_order(self, order: SalesOrder, customer_ref: str) -> str:
        values = {
            "partner_id": int(customer_ref),
            "client_order_ref": order.external_id,
            "origin": f"Salesforce: {order.name}",
            "order_line": self._order_lines(order),
        }
        return str(self._execute("sale.order", "create", values))

    def update_order(self, ref: str, order: SalesOrder, customer_ref: str) -> None:
        # (5, 0, 0) clears existing lines; the CRM line set replaces it wholesale.
        lines: list[Any] = [(5, 0, 0), *self._order_lines(order)]
        self._execute("sale.order", "write", [int(ref)],
                      {"partner_id": int(customer_ref), "order_line": lines})

    def list_status_changes(self, since: datetime | None) -> Iterator[ErpOrderStatus]:
        # Payments update the invoice (account.move), not the sale.order, so we
        # must watch both to notice "Paid".
        changed_names: dict[str, datetime] = {}
        stamp = [("write_date", ">=", _odoo_dt(since))] if since else []

        for order in self._execute("sale.order", "search_read",
                                   [("client_order_ref", "!=", False), *stamp],
                                   fields=["name", "write_date"]):
            changed_names[order["name"]] = _parse_odoo_dt(order["write_date"])
        for move in self._execute("account.move", "search_read",
                                  [("move_type", "=", "out_invoice"),
                                   ("invoice_origin", "!=", False), *stamp],
                                  fields=["invoice_origin", "write_date"]):
            ts = _parse_odoo_dt(move["write_date"])
            for name in move["invoice_origin"].split(", "):
                changed_names[name] = max(ts, changed_names.get(name, ts))

        if not changed_names:
            return
        orders = self._execute("sale.order", "search_read",
                               [("name", "in", list(changed_names))],
                               fields=["id", "name", "client_order_ref", "invoice_ids"])
        orders = [o for o in orders if o["client_order_ref"]]
        invoice_ids = [i for o in orders for i in o["invoice_ids"]]
        invoices = {
            inv["id"]: inv
            for inv in (self._execute("account.move", "read", invoice_ids,
                                      fields=["state", "move_type", "amount_total",
                                              "amount_residual"])
                        if invoice_ids else [])
        }

        statuses = []
        for o in orders:
            own = [invoices[i] for i in o["invoice_ids"]
                   if i in invoices and invoices[i]["move_type"] == "out_invoice"]
            status, due = derive_invoice_status(own)
            statuses.append(ErpOrderStatus(
                external_id=o["client_order_ref"], erp_order_ref=o["name"],
                invoice_status=status, amount_due=due, updated_at=changed_names[o["name"]],
            ))
        yield from sorted(statuses, key=lambda s: s.updated_at)
