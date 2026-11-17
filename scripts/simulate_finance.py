"""Play the Finance team in the local Odoo, so write-back and analytics have history.

Works through the real Odoo API (and reads deal close dates from Salesforce). For
every synced order it derives, deterministically from the order name, what Finance
would have done by today:

- customer payment terms: Net 30 by default, Net 45 for the two largest accounts,
  Net 15 for one smaller account
- order confirmed one day after the deal closed (deals from the last 2 days stay
  quotations, so CRM edits still flow through)
- invoice dated 2-9 days after close (fulfillment lag); not yet invoiced if that is
  in the future
- customer payments according to the customer's payment behavior (fast, normal,
  slow, chronically late); ~15% of invoices are paid in two installments
- one shipment to Twin Cities Trailhead arrives damaged and is fully credited

Each step only runs if the order is not already past it, so re-running is safe and
moves the book forward as time passes. Usage: uv run python scripts/simulate_finance.py
"""

import random
import xmlrpc.client
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from o2c_sync.config import Settings
from o2c_sync.connectors.salesforce import sf_cli_session

CENT = Decimal("0.01")
TERMS = {"Rocky Mountain Trail Supply": "45 Days", "Pacific Crest Provisions": "45 Days",
         "Twin Cities Trailhead": "15 Days"}
DEFAULT_TERM = "30 Days"
BEHAVIOR = {  # days from invoice to payment
    "fast": (8, 20), "normal": (22, 38), "slow": (35, 60), "late": (50, 90)}
PROFILE = {"Pacific Crest Provisions": "fast", "Blue Ridge Outfitters": "fast",
           "Sierra Summit Sports": "fast", "Desert Sky Expeditions": "slow",
           "Lone Star Basecamp": "slow", "Hudson Valley Camping Co.": "late"}
CREDITED_CUSTOMER = "Twin Cities Trailhead"


class Odoo:
    def __init__(self, s: Settings) -> None:
        self.db, self.pw = s.odoo_db, s.odoo_password.get_secret_value()
        common = xmlrpc.client.ServerProxy(f"{s.odoo_url}/xmlrpc/2/common", allow_none=True)
        self.models = xmlrpc.client.ServerProxy(f"{s.odoo_url}/xmlrpc/2/object", allow_none=True)
        self.uid = common.authenticate(self.db, s.odoo_username, self.pw, {})

    def __call__(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return self.models.execute_kw(self.db, self.uid, self.pw, model, method,
                                          list(args), kwargs)
        except xmlrpc.client.Fault as exc:
            # Wizard actions return None, which XML-RPC can't marshal; the work is done.
            if "cannot marshal None" in exc.faultString:
                return None
            raise


def close_dates(s: Settings) -> dict[str, date]:
    if not s.sf_cli_org:
        raise SystemExit("set O2C_SF_CLI_ORG to read deal close dates from Salesforce")
    sf = sf_cli_session(s.sf_cli_org)
    rows = sf.query_all("SELECT Id, CloseDate FROM Opportunity WHERE IsWon = true")["records"]
    return {r["Id"]: date.fromisoformat(r["CloseDate"]) for r in rows}


def plan(order_name: str, customer: str, close: date, total: Decimal) -> dict[str, Any]:
    rng = random.Random(order_name)
    invoice_date = close + timedelta(days=rng.randint(2, 9))
    low, high = BEHAVIOR[PROFILE.get(customer, "normal")]
    pay_date = invoice_date + timedelta(days=rng.randint(low, high))
    if rng.random() < 0.15:  # two installments
        first = (total * Decimal("0.5")).quantize(CENT)
        installments = [(pay_date, first), (pay_date + timedelta(days=21), total - first)]
    else:
        installments = [(pay_date, total)]
    return {"confirm_date": close + timedelta(days=1), "invoice_date": invoice_date,
            "installments": installments}


def main() -> None:
    s = Settings()
    odoo, today = Odoo(s), date.today()
    closes = close_dates(s)
    term_ids = {t["name"]: t["id"] for t in odoo("account.payment.term", "search_read", [],
                                                 fields=["name"])}
    orders = odoo("sale.order", "search_read", [("client_order_ref", "!=", False)],
                  fields=["id", "name", "state", "amount_total", "client_order_ref",
                          "partner_id", "invoice_ids"])
    # Oldest deals first, so invoice numbers ascend with the invoice date.
    orders.sort(key=lambda o: (closes.get(o["client_order_ref"], today), o["id"]))
    credited_done = False
    counts: dict[str, int] = {}

    for order in orders:
        close = closes.get(order["client_order_ref"])
        if close is None:
            continue
        customer = order["partner_id"][1]
        oid, total = order["id"], Decimal(str(order["amount_total"])).quantize(CENT)
        p = plan(order["name"], customer, close, total)

        # Finance maintains the customer's payment terms in the ERP.
        term_id = term_ids[TERMS.get(customer, DEFAULT_TERM)]
        odoo("res.partner", "write", [order["partner_id"][0]],
             {"property_payment_term_id": term_id})

        if p["confirm_date"] > today - timedelta(days=1):
            counts["quotation"] = counts.get("quotation", 0) + 1
            continue
        if order["state"] in ("draft", "sent"):
            odoo("sale.order", "action_confirm", [oid])
            # action_confirm stamps "now"; backdate to when Finance confirmed it.
            odoo("sale.order", "write", [oid], {"date_order": f"{p['confirm_date']} 15:00:00"})

        if p["invoice_date"] > today:
            counts["confirmed"] = counts.get("confirmed", 0) + 1
            continue
        if not order["invoice_ids"]:
            ctx = {"active_model": "sale.order", "active_ids": [oid], "active_id": oid}
            wiz = odoo("sale.advance.payment.inv", "create",
                       {"advance_payment_method": "delivered"}, context=ctx)
            odoo("sale.advance.payment.inv", "create_invoices", [wiz], context=ctx)
            order["invoice_ids"] = odoo("sale.order", "read", [oid],
                                        fields=["invoice_ids"])[0]["invoice_ids"]
        moves = odoo("account.move", "read", order["invoice_ids"],
                     fields=["id", "name", "state", "move_type", "amount_total",
                             "amount_residual", "invoice_date"])
        inv = next(m for m in moves if m["move_type"] == "out_invoice")
        if inv["state"] == "draft":
            # The invoice inherits the order's (empty) payment term; without a term
            # Odoo leaves the due date at the posting day instead of invoice date + terms.
            odoo("account.move", "write", [inv["id"]],
                 {"invoice_date": str(p["invoice_date"]), "invoice_payment_term_id": term_id})
            odoo("account.move", "action_post", [inv["id"]])
            inv = odoo("account.move", "read", [inv["id"]],
                       fields=["id", "name", "state", "amount_total", "amount_residual",
                               "invoice_date"])[0]

        refunds = [m for m in moves if m["move_type"] == "out_refund"]
        if customer == CREDITED_CUSTOMER and (refunds or not credited_done):
            credited_done = True
            if not refunds and Decimal(str(inv["amount_residual"])) == total:
                credit_date = p["invoice_date"] + timedelta(days=9)
                ctx = {"active_model": "account.move", "active_ids": [inv["id"]]}
                [journal] = odoo("account.move", "read", [inv["id"]], fields=["journal_id"])
                wiz = odoo("account.move.reversal", "create",
                           {"move_ids": [(6, 0, [inv["id"]])],
                            "journal_id": journal["journal_id"][0],
                            "reason": "Shipment arrived damaged; full credit",
                            "date": str(min(credit_date, today))},
                           context=ctx)
                odoo("account.move.reversal", "refund_moves", [wiz], context=ctx)
                refunds = odoo("account.move", "search_read",
                               [("reversed_entry_id", "=", inv["id"])], fields=["id", "state"])
            # Odoo 17 leaves the credit note as a draft; Finance posts it, which
            # reconciles it against the open invoice.
            for refund in refunds:
                if refund["state"] == "draft":
                    odoo("account.move", "action_post", [refund["id"]])
            counts["credited"] = counts.get("credited", 0) + 1
            continue

        paid = total - Decimal(str(inv["amount_residual"])).quantize(CENT)
        cumulative = Decimal(0)
        for pay_date, amount in p["installments"]:
            cumulative += amount
            if pay_date > today or cumulative <= paid:
                continue
            ctx = {"active_model": "account.move", "active_ids": [inv["id"]]}
            wiz = odoo("account.payment.register", "create",
                       {"amount": float(cumulative - paid), "payment_date": str(pay_date)},
                       context=ctx)
            odoo("account.payment.register", "action_create_payments", [wiz], context=ctx)
            paid = cumulative
        state = "paid" if paid >= total else "partial" if paid > 0 else "open"
        counts[state] = counts.get(state, 0) + 1

    print("orders by finance state:", dict(sorted(counts.items())))


if __name__ == "__main__":
    main()
