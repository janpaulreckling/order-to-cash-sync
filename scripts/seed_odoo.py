"""Prepare the local Odoo ERP for the demo. Safe to run repeatedly.

- Renames the company to Northwind Outfitters (US, USD).
- Loads the product master from config/product_catalog.csv (SKU -> default_code).
  The ERP owns the product master, so the sync never creates products itself.
- Products carry no default sales tax, so ERP invoice totals equal the CRM deal
  amount (keeps the downstream cash reconciliation free of tax noise).

Usage: uv run python scripts/seed_odoo.py
"""

import csv
import xmlrpc.client
from pathlib import Path
from typing import Any

from o2c_sync.config import Settings

CATALOG = Path(__file__).resolve().parent.parent / "config" / "product_catalog.csv"


def main() -> None:
    s = Settings()
    password = s.odoo_password.get_secret_value()
    common = xmlrpc.client.ServerProxy(f"{s.odoo_url}/xmlrpc/2/common", allow_none=True)
    models = xmlrpc.client.ServerProxy(f"{s.odoo_url}/xmlrpc/2/object", allow_none=True)
    uid = common.authenticate(s.odoo_db, s.odoo_username, password, {})
    if not uid:
        raise SystemExit("Odoo authentication failed")

    def call(model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        return models.execute_kw(s.odoo_db, uid, password, model, method, list(args), kwargs)

    [company] = call("res.company", "search_read", [], fields=["id", "currency_id"], limit=1)
    [us] = call("res.country", "search", [("code", "=", "US")], limit=1)
    call("res.company", "write", [company["id"]],
         {"name": "Northwind Outfitters", "country_id": us})
    print(f"company: Northwind Outfitters, currency {company['currency_id'][1]}")

    created = updated = 0
    with CATALOG.open(newline="") as fh:
        for row in csv.DictReader(fh):
            values: dict[str, Any] = {
                "name": row["name"],
                "default_code": row["sku"],
                "list_price": float(row["list_price"]),
                "type": "consu",
                "sale_ok": True,
                "taxes_id": [(6, 0, [])],
            }
            ids = call("product.product", "search", [("default_code", "=", row["sku"])],
                       limit=1)
            if ids:
                call("product.product", "write", ids, values)
                updated += 1
            else:
                call("product.product", "create", values)
                created += 1
    print(f"products: {created} created, {updated} updated")


if __name__ == "__main__":
    main()
