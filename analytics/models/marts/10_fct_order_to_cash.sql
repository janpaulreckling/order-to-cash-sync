-- One row per won deal: the full order-to-cash timeline across CRM and ERP.
CREATE OR REPLACE TABLE marts.fct_order_to_cash AS
WITH billing AS (
    SELECT
        order_name,
        MIN(invoice_date) FILTER (WHERE move_type = 'out_invoice')  AS invoice_date,
        MIN(due_date)     FILTER (WHERE move_type = 'out_invoice')  AS due_date,
        MIN(terms_days)   FILTER (WHERE move_type = 'out_invoice')  AS terms_days,
        SUM(sign * amount_total)                                    AS net_billed,
        SUM(sign * amount_residual)                                 AS net_due,
        BOOL_OR(move_type = 'out_refund')                           AS has_credit_note
    FROM staging.invoices
    GROUP BY order_name
),
payments AS (
    SELECT
        i.order_name,
        p.payment_date,
        p.amount,
        SUM(p.amount) OVER (PARTITION BY i.order_name ORDER BY p.payment_date, p.payment_id)
            AS cumulative_paid
    FROM staging.payments p
    JOIN staging.invoices i ON i.move_id = p.move_id AND i.move_type = 'out_invoice'
),
cash AS (
    SELECT
        p.order_name,
        SUM(p.amount)          AS cash_received,
        MIN(p.payment_date)    AS first_payment_date,
        MIN(p.payment_date) FILTER (WHERE p.cumulative_paid >= b.net_billed - 0.01)
                               AS fully_paid_date
    FROM payments p
    JOIN billing b USING (order_name)
    GROUP BY p.order_name
)
SELECT
    o.opportunity_id,
    o.opportunity_name,
    o.customer,
    o.close_date,
    o.amount                                    AS crm_amount,
    o.crm_invoice_status,
    o.crm_amount_due,
    so.order_name,
    so.order_state,
    so.order_amount,
    so.confirmed_date,
    b.invoice_date,
    b.due_date,
    b.terms_days,
    COALESCE(b.net_billed, 0)                   AS net_billed,
    COALESCE(b.net_due, 0)                      AS net_due,
    COALESCE(b.has_credit_note, FALSE)          AS has_credit_note,
    COALESCE(c.cash_received, 0)                AS cash_received,
    c.first_payment_date,
    c.fully_paid_date,
    CASE
        WHEN so.order_name IS NULL      THEN 'Not in ERP'
        WHEN b.invoice_date IS NULL     THEN 'Not Invoiced'
        WHEN b.net_billed <= 0          THEN 'Credited'
        WHEN b.net_due <= 0             THEN 'Paid'
        WHEN b.net_due < b.net_billed   THEN 'Partially Paid'
        ELSE 'Invoiced'
    END                                         AS erp_status,
    date_diff('day', o.close_date, b.invoice_date)        AS days_won_to_invoice,
    date_diff('day', b.invoice_date, c.fully_paid_date)   AS days_invoice_to_cash,
    date_diff('day', o.close_date, c.fully_paid_date)     AS days_won_to_cash,
    date_diff('day', b.due_date, c.fully_paid_date)       AS days_paid_vs_due
FROM staging.opportunities o
LEFT JOIN staging.orders so ON so.opportunity_id = o.opportunity_id
LEFT JOIN billing b         ON b.order_name = so.order_name
LEFT JOIN cash c            ON c.order_name = so.order_name
WHERE o.is_won;
