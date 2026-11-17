"""Seed the Salesforce Developer Edition org with a realistic Northwind demo book.

Creates, through the real REST API and idempotently (safe to re-run):
- Product2 + standard price book entries from config/product_catalog.csv
- one extra product that does NOT exist in the ERP (GIFT-CARD-100), so a won deal
  with it demonstrates the "ERP owns the product master" dead-letter path
- wholesale customer accounts (US outdoor retailers)
- opportunities with line items: most Closed Won, one still open, one Closed Lost

Usage: uv run python scripts/seed_salesforce.py
"""

import csv
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from simple_salesforce.api import Salesforce

from o2c_sync.config import Settings
from o2c_sync.connectors.salesforce import sf_cli_session

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "config" / "product_catalog.csv"
NOT_IN_ERP = {"sku": "GIFT-CARD-100", "name": "Northwind Gift Card $100", "list_price": "100.00"}

# State/country picklists are on in new orgs: write ISO codes. The sync reads
# Account.BillingCountry back as the label ("United States").
ACCOUNTS = [
    {"Name": "Rocky Mountain Trail Supply", "Phone": "(303) 555-0142",
     "BillingStreet": "1820 Wazee St", "BillingCity": "Denver", "BillingStateCode": "CO",
     "BillingPostalCode": "80202", "BillingCountryCode": "US"},
    {"Name": "Cascade Paddle Co.", "Phone": "(206) 555-0187",
     "BillingStreet": "411 Westlake Ave N", "BillingCity": "Seattle", "BillingStateCode": "WA",
     "BillingPostalCode": "98109", "BillingCountryCode": "US"},
    {"Name": "Blue Ridge Outfitters", "Phone": "(828) 555-0119",
     "BillingStreet": "36 Biltmore Ave", "BillingCity": "Asheville", "BillingStateCode": "NC",
     "BillingPostalCode": "28801", "BillingCountryCode": "US"},
    {"Name": "Great Lakes Adventure Gear", "Phone": "(312) 555-0163",
     "BillingStreet": "222 W Ontario St", "BillingCity": "Chicago", "BillingStateCode": "IL",
     "BillingPostalCode": "60654", "BillingCountryCode": "US"},
    {"Name": "Desert Sky Expeditions", "Phone": "(602) 555-0128",
     "BillingStreet": "4 N Central Ave", "BillingCity": "Phoenix", "BillingStateCode": "AZ",
     "BillingPostalCode": "85004", "BillingCountryCode": "US"},
    {"Name": "Hudson Valley Camping Co.", "Phone": "(845) 555-0175",
     "BillingStreet": "301 Wall St", "BillingCity": "Kingston", "BillingStateCode": "NY",
     "BillingPostalCode": "12401", "BillingCountryCode": "US"},
]

# (opportunity name, account, stage, days ago closed, [(sku, qty, unit price)])
OPPORTUNITIES: list[tuple[str, str, str, int, list[tuple[str, int, float]]]] = [
    ("RMTS - Spring Backpacking Restock", "Rocky Mountain Trail Supply", "Closed Won", 21,
     [("TENT-2P", 12, 315.00), ("PACK-45L", 20, 170.00), ("STOVE-1", 25, 71.00)]),
    ("RMTS - Sleep System Bundle", "Rocky Mountain Trail Supply", "Closed Won", 9,
     [("BAG-20F", 15, 269.00), ("PAD-INSUL", 15, 116.00)]),
    ("Cascade - Touring Kayak Fleet", "Cascade Paddle Co.", "Closed Won", 18,
     [("KAYAK-X", 6, 1124.00), ("PADDLE-CF", 12, 197.00)]),
    ("Blue Ridge - Family Camping Season", "Blue Ridge Outfitters", "Closed Won", 14,
     [("TENT-4P", 10, 476.00), ("LAMP-HEAD", 40, 35.00), ("FILTER-H2O", 18, 89.00)]),
    ("Great Lakes - Rainwear Program", "Great Lakes Adventure Gear", "Closed Won", 7,
     [("JACKET-RAIN", 60, 152.00)]),
    ("Desert Sky - Expedition Packs", "Desert Sky Expeditions", "Closed Won", 5,
     [("PACK-65L", 16, 233.00), ("FILTER-H2O", 16, 89.00), ("LAMP-HEAD", 16, 35.00)]),
    ("Hudson Valley - Holiday Gift Cards", "Hudson Valley Camping Co.", "Closed Won", 3,
     [("GIFT-CARD-100", 50, 100.00), ("LAMP-HEAD", 30, 35.00)]),  # SKU not in ERP
    ("Hudson Valley - Winter Tents", "Hudson Valley Camping Co.", "Negotiation/Review", -10,
     [("TENT-4P", 8, 489.00)]),  # still open: must not sync
    ("Cascade - Paddle Upgrade", "Cascade Paddle Co.", "Closed Lost", 12,
     [("PADDLE-CF", 20, 205.00)]),  # lost: must not sync
]


def connect(s: Settings) -> Salesforce:
    if s.sf_cli_org:
        return sf_cli_session(s.sf_cli_org)
    if not (s.sf_username and s.sf_password and s.sf_security_token):
        raise SystemExit("Salesforce credentials missing; see .env.example")
    return Salesforce(username=s.sf_username, password=s.sf_password.get_secret_value(),
                      security_token=s.sf_security_token.get_secret_value(), domain=s.sf_domain)


def q(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def find_one(sf: Salesforce, soql: str) -> dict[str, Any] | None:
    records = sf.query(soql)["records"]
    return records[0] if records else None


def seed_products(sf: Salesforce) -> dict[str, str]:
    """Product2 + standard price book entry per SKU. Returns SKU -> PricebookEntry Id."""
    std = find_one(sf, "SELECT Id, IsActive FROM Pricebook2 WHERE IsStandard = true")
    assert std, "org has no standard price book"
    if not std["IsActive"]:
        sf.Pricebook2.update(std["Id"], {"IsActive": True})  # type: ignore[operator]

    with CATALOG.open(newline="") as fh:
        rows = [*csv.DictReader(fh), NOT_IN_ERP]
    entries = {}
    for row in rows:
        product = find_one(sf, f"SELECT Id FROM Product2 WHERE ProductCode = '{q(row['sku'])}'")
        values = {"Name": row["name"], "ProductCode": row["sku"], "IsActive": True,
                  "Family": "Gift Cards" if row["sku"].startswith("GIFT") else "Outdoor Gear"}
        if product:
            sf.Product2.update(product["Id"], values)  # type: ignore[operator]
            product_id = product["Id"]
        else:
            product_id = sf.Product2.create(values)["id"]  # type: ignore[operator]
        pbe = find_one(sf, f"SELECT Id FROM PricebookEntry WHERE Pricebook2Id = '{std['Id']}' "
                           f"AND Product2Id = '{product_id}'")
        price = {"UnitPrice": float(row["list_price"]), "IsActive": True}
        if pbe:
            sf.PricebookEntry.update(pbe["Id"], price)  # type: ignore[operator]
            entries[row["sku"]] = pbe["Id"]
        else:
            entries[row["sku"]] = sf.PricebookEntry.create(  # type: ignore[operator]
                {"Pricebook2Id": std["Id"], "Product2Id": product_id, **price})["id"]
    print(f"products: {len(entries)} with standard price book entries")
    return entries


def seed_accounts(sf: Salesforce) -> dict[str, str]:
    ids = {}
    for acct in ACCOUNTS:
        found = find_one(sf, f"SELECT Id FROM Account WHERE Name = '{q(acct['Name'])}'")
        if found:
            sf.Account.update(found["Id"], acct)  # type: ignore[operator]
            ids[acct["Name"]] = found["Id"]
        else:
            ids[acct["Name"]] = sf.Account.create(acct)["id"]  # type: ignore[operator]
    print(f"accounts: {len(ids)}")
    return ids


def seed_opportunities(sf: Salesforce, accounts: dict[str, str], pbes: dict[str, str]) -> None:
    std = find_one(sf, "SELECT Id FROM Pricebook2 WHERE IsStandard = true")
    assert std
    created = 0
    for name, account, stage, days_ago, lines in OPPORTUNITIES:
        if find_one(sf, f"SELECT Id FROM Opportunity WHERE Name = '{q(name)}'"):
            continue  # never rewrite existing deals: that would re-trigger the sync
        close = (date.today() - timedelta(days=days_ago)).isoformat()
        # Line items first while the deal is open, then close it: Amount is the
        # roll-up of the line items, exactly as a rep would do it in the UI.
        opp_id = sf.Opportunity.create({  # type: ignore[operator]
            "Name": name, "AccountId": accounts[account], "StageName": "Prospecting",
            "CloseDate": close, "Pricebook2Id": std["Id"]})["id"]
        for sku, qty, price in lines:
            sf.OpportunityLineItem.create({  # type: ignore[operator]
                "OpportunityId": opp_id, "PricebookEntryId": pbes[sku],
                "Quantity": qty, "UnitPrice": price})
        if stage != "Prospecting":
            sf.Opportunity.update(opp_id, {"StageName": stage})  # type: ignore[operator]
        created += 1
    print(f"opportunities: {created} created, {len(OPPORTUNITIES) - created} already present")


def main() -> None:
    sf = connect(Settings())
    pbes = seed_products(sf)
    accounts = seed_accounts(sf)
    seed_opportunities(sf, accounts, pbes)


if __name__ == "__main__":
    main()
