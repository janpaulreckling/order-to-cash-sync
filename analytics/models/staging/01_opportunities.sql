-- Salesforce opportunities, renamed to business terms. One row per opportunity.
CREATE OR REPLACE VIEW staging.opportunities AS
SELECT
    id                  AS opportunity_id,
    name                AS opportunity_name,
    account_name        AS customer,
    stage,
    is_won,
    is_closed,
    amount,
    close_date,
    created_at,
    erp_order_ref,
    erp_invoice_status  AS crm_invoice_status,
    erp_amount_due      AS crm_amount_due
FROM raw.sf_opportunities;
