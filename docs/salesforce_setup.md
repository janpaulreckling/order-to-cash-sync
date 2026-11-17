# Salesforce setup

## 1. Get a free org

Sign up for a Developer Edition at <https://developer.salesforce.com/signup>. It is free, does not expire while you use it, and has the full REST API.

## 2. Security token

Avatar → **Settings** → **Reset My Security Token**. The token arrives by email. Put it in `.env` as `O2C_SF_SECURITY_TOKEN`.

## 3. Write-back fields on Opportunity

Setup → Object Manager → **Opportunity** → Fields & Relationships → New:

| Label | API name | Type |
|---|---|---|
| ERP Order Ref | `ERP_Order_Ref__c` | Text(40) |
| ERP Invoice Status | `ERP_Invoice_Status__c` | Picklist: Not Invoiced, Invoiced, Partially Paid, Paid |
| ERP Amount Due | `ERP_Amount_Due__c` | Currency(16, 2) |
| ERP Last Sync | `ERP_Last_Sync__c` | Date/Time |

Make them **read-only on the page layout**: they are owned by the integration, and reps should not edit them.

## 4. Products

Every Product2 used on an opportunity needs a **Product Code** that matches a product's **Internal Reference** in Odoo. Unmatched SKUs are dead-lettered by design, because the ERP owns the product master.

## 5. Smoke test

```bash
uv run o2c sync --dry-run
```
