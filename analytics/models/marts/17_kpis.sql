-- Headline numbers for the dashboard. DSO uses the last 90 days of billings.
CREATE OR REPLACE TABLE marts.kpis AS
SELECT
    (SELECT COUNT(*) FROM marts.fct_order_to_cash)                          AS deals_won,
    (SELECT SUM(crm_amount) FROM marts.fct_order_to_cash)                   AS bookings,
    (SELECT SUM(amount) FROM staging.payments)                              AS collected,
    (SELECT SUM(amount_residual) FROM staging.invoices
      WHERE move_type = 'out_invoice')                                      AS open_ar,
    (SELECT SUM(amount_residual) FROM staging.invoices
      WHERE move_type = 'out_invoice' AND due_date < current_date)          AS overdue_ar,
    ROUND((SELECT SUM(amount_residual) FROM staging.invoices WHERE move_type = 'out_invoice')
        / NULLIF((SELECT SUM(sign * amount_total) FROM staging.invoices
                  WHERE invoice_date > current_date - 90), 0) * 90, 1)      AS dso_days,
    (SELECT MEDIAN(days_won_to_cash) FROM marts.fct_order_to_cash)          AS median_days_won_to_cash,
    (SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE days_paid_vs_due <= 0)
                  / NULLIF(COUNT(fully_paid_date), 0), 0)
       FROM marts.fct_order_to_cash)                                        AS pct_paid_on_time,
    (SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE check_status = 'ok') / COUNT(*), 1)
       FROM marts.crm_erp_reconciliation)                                   AS pct_deals_reconciled,
    (SELECT COUNT(*) FROM raw.sync_runs)                                    AS sync_runs,
    (SELECT COUNT(*) FROM raw.dead_letters WHERE resolved_at IS NULL)       AS open_dead_letters;
