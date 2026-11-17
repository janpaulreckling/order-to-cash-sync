"""Win a new deal in Salesforce (real API), for the README demo recording.

Usage: uv run python scripts/demo_new_deal.py
"""

from datetime import date, datetime

from seed_salesforce import connect, find_one

from o2c_sync.config import Settings

LINES = [("TENT-2P", 10, 315.00), ("PACK-45L", 12, 170.00)]


def main() -> None:
    sf = connect(Settings())
    account = find_one(sf, "SELECT Id, Name FROM Account WHERE Name = 'Pacific Crest Provisions'")
    std = find_one(sf, "SELECT Id FROM Pricebook2 WHERE IsStandard = true")
    assert account and std
    name = f"Pacific Crest - Live Demo {datetime.now():%H:%M}"
    opp_id = sf.Opportunity.create({  # type: ignore[operator]
        "Name": name, "AccountId": account["Id"], "StageName": "Prospecting",
        "CloseDate": date.today().isoformat(), "Pricebook2Id": std["Id"]})["id"]
    for sku, qty, price in LINES:
        pbe = find_one(sf, f"SELECT Id FROM PricebookEntry WHERE Pricebook2Id = '{std['Id']}' "
                           f"AND ProductCode = '{sku}'")
        assert pbe
        sf.OpportunityLineItem.create({  # type: ignore[operator]
            "OpportunityId": opp_id, "PricebookEntryId": pbe["Id"],
            "Quantity": qty, "UnitPrice": price})
    sf.Opportunity.update(opp_id, {"StageName": "Closed Won"})  # type: ignore[operator]
    total = sum(q * p for _, q, p in LINES)
    print(f"Salesforce: '{name}' is Closed Won, ${total:,.2f}")


if __name__ == "__main__":
    main()
