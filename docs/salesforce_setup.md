# Salesforce setup

Everything the sync needs in Salesforce is code in this repo: the custom fields and the permission set are metadata under [`salesforce/`](../salesforce), and the demo data comes from a seed script. Nothing has to be clicked together in Setup.

## 1. Get a free org

Sign up for a Developer Edition at <https://developer.salesforce.com/signup>. It is free and has the full REST API. It expires after 45 days without use.

## 2. Authorize the Salesforce CLI (OAuth)

```bash
npm install -g @salesforce/cli
sf org login web --instance-url https://<your-domain>.my.salesforce.com --alias northwind
```

Set `O2C_SF_CLI_ORG=northwind` in `.env`. The sync gets its access token from the CLI (`sf org auth show-access-token`), so no password or security token is stored in the project. The CLI refreshes the token, and the connector re-authenticates when a session expires.

*Alternative:* username + password + security token (`O2C_SF_USERNAME`, `O2C_SF_PASSWORD`, `O2C_SF_SECURITY_TOKEN`). In production, the right choice is the OAuth JWT bearer flow with a dedicated integration user.

## 3. Deploy the write-back fields

```bash
cd salesforce
sf project deploy start --target-org northwind --source-dir force-app
sf org assign permset --name O2C_Integration --target-org northwind
```

| Label | API name | Type |
|---|---|---|
| ERP Order Ref | `ERP_Order_Ref__c` | Text(40) |
| ERP Invoice Status | `ERP_Invoice_Status__c` | Picklist (restricted): Not Invoiced, Invoiced, Partially Paid, Paid |
| ERP Amount Due | `ERP_Amount_Due__c` | Currency(16, 2) |
| ERP Last Sync | `ERP_Last_Sync__c` | Date/Time |

Fields deployed through the Metadata API have no field-level security, so the `O2C_Integration` permission set grants read/edit on them to the sync user. Reps should only get read access: the integration owns these fields.

## 4. Seed demo data

```bash
uv run python scripts/seed_salesforce.py
```

This creates products with standard price book entries (Product Code = Odoo Internal Reference, from `config/product_catalog.csv`), six wholesale accounts and nine opportunities. Seven are Closed Won, one is open and one is lost. One won deal contains `GIFT-CARD-100`, which doesn't exist in the ERP on purpose, to show the dead-letter path. The script is idempotent.

## 5. Run it

```bash
uv run o2c sync --dry-run
uv run o2c sync
uv run python scripts/simulate_finance.py   # Finance confirms, invoices, books payments in Odoo
uv run o2c sync                             # statuses flow back onto the opportunities
```

## Org quirks found against a real org

- **No `OpportunityLineItem.Discount`** in new Developer Edition orgs. Negotiated prices arrive as `UnitPrice`, and the mapping leaves `discount_pct` commented out.
- **State and country picklists** are on by default. Writes need `BillingStateCode` / `BillingCountryCode`. Reads of `BillingCountry` return the label ("United States").
- **Changing `Quantity` over the API keeps `TotalPrice`** and recalculates `UnitPrice` to 8 decimals. Odoo rounds unit prices to the cent, so the ERP total can drift by a few cents. The connector reconciles the ERP total after every write and dead-letters any mismatch.
