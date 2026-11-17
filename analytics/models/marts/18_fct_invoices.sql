-- Posted invoices and credit notes at document level, signed so they sum to net billings.
-- The dashboard rebuilds receivables "as of" any date from this table and fct_payments.
CREATE OR REPLACE TABLE marts.fct_invoices AS
SELECT
    i.move_id,
    i.document_no,
    i.move_type,
    i.order_name,
    i.customer,
    i.invoice_date,
    i.due_date,
    i.sign * i.amount_total     AS amount_signed,
    i.amount_residual,
    i.reversed_entry_id
FROM staging.invoices i;
