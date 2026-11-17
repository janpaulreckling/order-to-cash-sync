"""Play the Finance team in the local Odoo, so the write-back has something to report.

Through the real Odoo API, for the synced Salesforce orders (oldest first):
  1st order: confirm, invoice, customer pays in full        -> Paid
  2nd order: confirm, invoice, customer pays 40%            -> Partially Paid
  3rd order: confirm, invoice, no payment yet               -> Invoiced
  4th order: confirm, invoice, customer pays in full        -> Paid
  5th order: confirm only (not invoiced yet)                -> Not Invoiced
  others:    left as quotations, so CRM edits still flow through

Each step only runs if the order is not already past it, so re-running is safe.
Usage: uv run python scripts/simulate_finance.py
"""

import xmlrpc.client
from decimal import Decimal
from typing import Any

from o2c_sync.config import Settings

PLAN = ["paid", "partial", "invoiced", "paid", "confirmed"]
PARTIAL_SHARE = Decimal("0.40")


def main() -> None:
    s = Settings()
    password = s.odoo_password.get_secret_value()
    common = xmlrpc.client.ServerProxy(f"{s.odoo_url}/xmlrpc/2/common", allow_none=True)
    models = xmlrpc.client.ServerProxy(f"{s.odoo_url}/xmlrpc/2/object", allow_none=True)
    uid = common.authenticate(s.odoo_db, s.odoo_username, password, {})

    def call(model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return models.execute_kw(s.odoo_db, uid, password, model, method, list(args), kwargs)
        except xmlrpc.client.Fault as exc:
            # Wizard actions return None, which XML-RPC can't marshal; the work is done.
            if "cannot marshal None" in exc.faultString:
                return None
            raise

    orders = call("sale.order", "search_read", [("client_order_ref", "!=", False)],
                  fields=["id", "name", "state", "amount_total"], order="id asc")
    for order, step in zip(orders, PLAN, strict=False):
        oid = order["id"]
        if order["state"] in ("draft", "sent"):
            call("sale.order", "action_confirm", [oid])
        if step == "confirmed":
            print(f"{order['name']}: confirmed")
            continue

        [so] = call("sale.order", "read", [oid], fields=["invoice_ids"])
        if not so["invoice_ids"]:
            ctx = {"active_model": "sale.order", "active_ids": [oid], "active_id": oid}
            wiz = call("sale.advance.payment.inv", "create",
                       {"advance_payment_method": "delivered"}, context=ctx)
            call("sale.advance.payment.inv", "create_invoices", [wiz], context=ctx)
            [so] = call("sale.order", "read", [oid], fields=["invoice_ids"])
        [inv] = call("account.move", "read", so["invoice_ids"][:1],
                     fields=["id", "name", "state", "amount_total", "amount_residual"])
        if inv["state"] == "draft":
            call("account.move", "action_post", [inv["id"]])
            [inv] = call("account.move", "read", [inv["id"]],
                         fields=["id", "name", "state", "amount_total", "amount_residual"])

        total, residual = Decimal(str(inv["amount_total"])), Decimal(str(inv["amount_residual"]))
        target_paid = {"paid": total, "partial": (total * PARTIAL_SHARE).quantize(Decimal("0.01")),
                       "invoiced": Decimal(0)}[step]
        to_pay = target_paid - (total - residual)
        if to_pay > 0:
            ctx = {"active_model": "account.move", "active_ids": [inv["id"]]}
            wiz = call("account.payment.register", "create", {"amount": float(to_pay)},
                       context=ctx)
            call("account.payment.register", "action_create_payments", [wiz], context=ctx)
        print(f"{order['name']}: invoice {inv['name']} {step} (paid {target_paid} of {total})")


if __name__ == "__main__":
    main()
