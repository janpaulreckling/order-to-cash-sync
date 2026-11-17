-- How each customer pays: days to pay, and days vs. their payment terms (negative = early).
CREATE OR REPLACE TABLE marts.customer_payment_behavior AS
SELECT
    customer,
    COUNT(*)                                                      AS deals_won,
    SUM(crm_amount)                                               AS revenue,
    ANY_VALUE(terms_days) FILTER (WHERE terms_days IS NOT NULL)   AS terms_days,
    COUNT(fully_paid_date)                                        AS invoices_paid,
    ROUND(AVG(days_invoice_to_cash), 1)                           AS avg_days_to_pay,
    ROUND(AVG(days_paid_vs_due), 1)                               AS avg_days_vs_terms,
    ROUND(100.0 * COUNT(*) FILTER (WHERE days_paid_vs_due > 0)
          / NULLIF(COUNT(fully_paid_date), 0), 0)                 AS pct_paid_late,
    SUM(net_due) FILTER (WHERE net_due > 0)                       AS open_ar
FROM marts.fct_order_to_cash
GROUP BY customer;
