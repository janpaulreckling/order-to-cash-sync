-- Customer payments with the invoice they settled.
CREATE OR REPLACE TABLE marts.fct_payments AS
SELECT
    p.payment_id,
    p.move_id,
    i.order_name,
    p.customer,
    p.payment_date,
    p.amount
FROM staging.payments p
LEFT JOIN staging.invoices i ON i.move_id = p.move_id;
