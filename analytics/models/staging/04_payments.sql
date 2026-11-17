-- Customer payments, one row per payment and the invoice it was reconciled with.
CREATE OR REPLACE VIEW staging.payments AS
SELECT
    p.id            AS payment_id,
    pi.move_id,
    p.payment_date,
    p.amount,
    p.partner_name  AS customer
FROM raw.odoo_payments p
JOIN raw.odoo_payment_invoices pi ON pi.payment_id = p.id
WHERE p.state = 'posted';
