-- ERP sales orders created by the sync (client_order_ref = Salesforce Opportunity Id).
CREATE OR REPLACE VIEW staging.orders AS
SELECT
    id                  AS order_id,
    name                AS order_name,
    client_order_ref    AS opportunity_id,
    partner_name        AS customer,
    state               AS order_state,
    amount_total        AS order_amount,
    CASE WHEN state IN ('sale', 'done') THEN CAST(date_order AS DATE) END AS confirmed_date
FROM raw.odoo_sale_orders
WHERE client_order_ref IS NOT NULL;
