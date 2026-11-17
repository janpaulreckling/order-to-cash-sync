-- Posted customer invoices and credit notes. Credit notes carry sign -1 so that
-- sums net out (a reversed invoice nets to zero instead of looking paid).
CREATE OR REPLACE VIEW staging.invoices AS
SELECT
    id                  AS move_id,
    name                AS document_no,
    move_type,
    CASE move_type WHEN 'out_refund' THEN -1 ELSE 1 END AS sign,
    invoice_origin      AS order_name,
    partner_name        AS customer,
    invoice_date,
    invoice_date_due    AS due_date,
    date_diff('day', invoice_date, invoice_date_due) AS terms_days,
    payment_term,
    amount_total,
    amount_residual,
    payment_state,
    reversed_entry_id
FROM raw.odoo_moves
WHERE state = 'posted';
