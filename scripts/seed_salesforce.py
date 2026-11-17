"""Seed the Salesforce Developer Edition org with a realistic Northwind demo book.

Creates, through the real REST API and idempotently (safe to re-run):
- Product2 + standard price book entries from config/product_catalog.csv
- one extra product that does NOT exist in the ERP (GIFT-CARD-100), so a won deal
  with it demonstrates the "ERP owns the product master" dead-letter path
- wholesale customer accounts (US outdoor retailers)
- opportunities with line items: most Closed Won, one still open, one Closed Lost
- ~6 months of generated order history (deterministic, anchored to a fixed date, so
  every re-run produces the same deals) for the analytics layer
- an additive holdout batch ("Holdout Reorder #01-25") invoiced Nov 1-15 and never paid,
  so the reconciliation project can measure matching quality on cases it was not tuned on
- an additive fall-volume batch (Oct 15 - Nov 14) so the November close has a realistic
  number of open invoices for the sibling cash-reconciliation project; its own RNG, so
  adding it never changes the deals above

Usage: uv run python scripts/seed_salesforce.py
"""

import csv
import random
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
    {"Name": "Green Mountain Gear Exchange", "Phone": "(802) 555-0131",
     "BillingStreet": "88 Church St", "BillingCity": "Burlington", "BillingStateCode": "VT",
     "BillingPostalCode": "05401", "BillingCountryCode": "US"},
    {"Name": "Lone Star Basecamp", "Phone": "(512) 555-0158",
     "BillingStreet": "600 Congress Ave", "BillingCity": "Austin", "BillingStateCode": "TX",
     "BillingPostalCode": "78701", "BillingCountryCode": "US"},
    {"Name": "Pacific Crest Provisions", "Phone": "(503) 555-0194",
     "BillingStreet": "1001 SW 5th Ave", "BillingCity": "Portland", "BillingStateCode": "OR",
     "BillingPostalCode": "97204", "BillingCountryCode": "US"},
    {"Name": "Sierra Summit Sports", "Phone": "(530) 555-0112",
     "BillingStreet": "10100 Donner Pass Rd", "BillingCity": "Truckee", "BillingStateCode": "CA",
     "BillingPostalCode": "96161", "BillingCountryCode": "US"},
    {"Name": "Chesapeake Kayak Outfitters", "Phone": "(410) 555-0147",
     "BillingStreet": "120 Compromise St", "BillingCity": "Annapolis", "BillingStateCode": "MD",
     "BillingPostalCode": "21401", "BillingCountryCode": "US"},
    {"Name": "Twin Cities Trailhead", "Phone": "(612) 555-0183",
     "BillingStreet": "250 Marquette Ave", "BillingCity": "Minneapolis", "BillingStateCode": "MN",
     "BillingPostalCode": "55401", "BillingCountryCode": "US"},
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


# ------------------------------------------------------------- generated history
HISTORY_ANCHOR = date(2026, 11, 16)  # fixed, so re-runs on later days create no new deals
HISTORY_START = date(2026, 5, 1)
SEGMENTS = {  # account -> (short name, product mix, orders per month)
    "Rocky Mountain Trail Supply": ("RMTS", ["TENT-2P", "PACK-45L", "STOVE-1", "BAG-20F"], 1.4),
    "Cascade Paddle Co.": ("Cascade", ["KAYAK-X", "PADDLE-CF", "JACKET-RAIN"], 0.9),
    "Blue Ridge Outfitters": ("Blue Ridge", ["TENT-4P", "LAMP-HEAD", "FILTER-H2O"], 1.2),
    "Great Lakes Adventure Gear": ("Great Lakes", ["JACKET-RAIN", "PACK-45L", "LAMP-HEAD"], 1.0),
    "Desert Sky Expeditions": ("Desert Sky", ["PACK-65L", "FILTER-H2O", "LAMP-HEAD"], 0.8),
    "Hudson Valley Camping Co.": ("Hudson Valley", ["TENT-4P", "BAG-20F", "STOVE-1"], 1.0),
    "Green Mountain Gear Exchange": ("Green Mtn", ["BAG-20F", "PAD-INSUL", "JACKET-RAIN"], 0.7),
    "Lone Star Basecamp": ("Lone Star", ["TENT-4P", "STOVE-1", "FILTER-H2O", "LAMP-HEAD"], 1.1),
    "Pacific Crest Provisions": ("Pacific Crest", ["PACK-65L", "TENT-2P", "PAD-INSUL"], 1.3),
    "Sierra Summit Sports": ("Sierra", ["BAG-20F", "TENT-2P", "PACK-65L"], 0.9),
    "Chesapeake Kayak Outfitters": ("Chesapeake", ["KAYAK-X", "PADDLE-CF"], 0.6),
    "Twin Cities Trailhead": ("Twin Cities", ["JACKET-RAIN", "LAMP-HEAD", "STOVE-1"], 0.8),
}
# Outdoor retail buys ahead of the summer season and again before the holidays.
SEASONALITY = {5: 1.3, 6: 1.2, 7: 0.9, 8: 0.8, 9: 0.9, 10: 1.1, 11: 1.0}
PROGRAMS = {5: "Summer Kickoff", 6: "Peak Season Refill", 7: "Midsummer Refill",
            8: "Back-to-School", 9: "Fall Camping", 10: "Holiday Pre-Book", 11: "Winter Restock"}


def history_opportunities() -> list[tuple[str, str, str, int, list[tuple[str, int, float]]]]:
    rng = random.Random(20261116)
    with CATALOG.open(newline="") as fh:
        list_price = {row["sku"]: float(row["list_price"]) for row in csv.DictReader(fh)}
    deals = []
    for account, (short, mix, per_month) in SEGMENTS.items():
        seq = 0
        month = HISTORY_START
        while month <= HISTORY_ANCHOR:
            expected = per_month * SEASONALITY[month.month]
            count = int(expected) + (rng.random() < expected % 1)
            for _ in range(count):
                close = month + timedelta(days=rng.randrange(0, 28))
                if close > HISTORY_ANCHOR - timedelta(days=1):
                    continue
                seq += 1
                skus = rng.sample(mix, k=rng.randint(1, min(3, len(mix))))
                lines = []
                for sku in skus:
                    big = list_price[sku] > 500
                    qty = rng.randint(2, 8) if big else rng.randint(8, 48)
                    discount = rng.choice([0.08, 0.10, 0.12, 0.15])  # wholesale tiers
                    lines.append((sku, qty, round(list_price[sku] * (1 - discount), 2)))
                stage = "Closed Lost" if rng.random() < 0.12 else "Closed Won"
                name = f"{short} - {PROGRAMS[month.month]} #{seq:02d}"
                deals.append((name, account, stage, (date.today() - close).days, lines))
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return deals


VOLUME_START, VOLUME_END = date(2026, 10, 15), date(2026, 11, 14)
VOLUME_DEALS = 30


def volume_opportunities() -> list[tuple[str, str, str, int, list[tuple[str, int, float]]]]:
    rng = random.Random("fall-volume-2026")
    with CATALOG.open(newline="") as fh:
        list_price = {row["sku"]: float(row["list_price"]) for row in csv.DictReader(fh)}
    accounts = list(SEGMENTS)
    span = (VOLUME_END - VOLUME_START).days
    deals = []
    for n in range(1, VOLUME_DEALS + 1):
        account = accounts[(n - 1) % len(accounts)]
        short, mix, _ = SEGMENTS[account]
        close = VOLUME_START + timedelta(days=rng.randrange(0, span + 1))
        lines = []
        for sku in rng.sample(mix, k=rng.randint(1, min(2, len(mix)))):
            qty = rng.randint(2, 6) if list_price[sku] > 500 else rng.randint(6, 30)
            discount = rng.choice([0.08, 0.10, 0.12, 0.15])
            lines.append((sku, qty, round(list_price[sku] * (1 - discount), 2)))
        deals.append((f"{short} - Fall Reorder #{n:02d}", account, "Closed Won",
                      (date.today() - close).days, lines))
    return deals


HOLDOUT_TAG = "Holdout Reorder"
# (account, lines) per deal. Deliberate shapes for the matcher: four customers with
# 3+ open invoices, one customer with two invoices of the same amount, small amounts.
HOLDOUT_SPEC: list[tuple[str, list[tuple[str, int, float]]]] = [
    ("Lone Star Basecamp", [("TENT-4P", 4, 465.52)]),
    ("Lone Star Basecamp", [("LAMP-HEAD", 8, 35.10)]),                       # small
    ("Lone Star Basecamp", [("STOVE-1", 12, 69.52), ("FILTER-H2O", 6, 89.10)]),
    ("Lone Star Basecamp", [("FILTER-H2O", 10, 87.12)]),
    ("Hudson Valley Camping Co.", [("BAG-20F", 6, 263.12)]),
    ("Hudson Valley Camping Co.", [("BAG-20F", 6, 263.12)]),                 # same amount
    ("Hudson Valley Camping Co.", [("STOVE-1", 5, 71.10)]),                  # small
    ("Hudson Valley Camping Co.", [("TENT-4P", 3, 476.10), ("STOVE-1", 6, 71.10)]),
    ("Sierra Summit Sports", [("PACK-65L", 7, 233.10)]),
    ("Sierra Summit Sports", [("TENT-2P", 9, 307.12)]),
    ("Sierra Summit Sports", [("PAD-INSUL", 3, 116.10)]),                    # small
    ("Green Mountain Gear Exchange", [("JACKET-RAIN", 14, 148.72)]),
    ("Green Mountain Gear Exchange", [("PAD-INSUL", 12, 113.52)]),
    ("Green Mountain Gear Exchange", [("BAG-20F", 4, 269.10)]),
]
HOLDOUT_FILL = 25 - len(HOLDOUT_SPEC)  # the rest: other customers, generated


def holdout_opportunities() -> list[tuple[str, str, str, int, list[tuple[str, int, float]]]]:
    rng = random.Random("holdout-2026")
    with CATALOG.open(newline="") as fh:
        list_price = {row["sku"]: float(row["list_price"]) for row in csv.DictReader(fh)}
    others = [a for a in SEGMENTS if a not in {acct for acct, _ in HOLDOUT_SPEC}]
    spec = list(HOLDOUT_SPEC)
    for i in range(HOLDOUT_FILL):
        account = others[i % len(others)]
        sku = rng.choice(SEGMENTS[account][1])
        qty = rng.randint(2, 5) if list_price[sku] > 500 else rng.randint(5, 25)
        spec.append((account, [(sku, qty, round(list_price[sku] * 0.9, 2))]))
    deals = []
    for n, (account, lines) in enumerate(spec, start=1):
        close = date(2026, 10, 30) + timedelta(days=rng.randrange(0, 14))  # invoiced Nov 1-15
        short = SEGMENTS[account][0]
        deals.append((f"{short} - {HOLDOUT_TAG} #{n:02d}", account, "Closed Won",
                      (date.today() - close).days, lines))
    return deals


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
    deals = (OPPORTUNITIES + history_opportunities() + volume_opportunities()
             + holdout_opportunities())
    for name, account, stage, days_ago, lines in deals:
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
    print(f"opportunities: {created} created, {len(deals) - created} already present")


def main() -> None:
    sf = connect(Settings())
    pbes = seed_products(sf)
    accounts = seed_accounts(sf)
    seed_opportunities(sf, accounts, pbes)


if __name__ == "__main__":
    main()
