-- Median days per stage of the order-to-cash cycle, by the month the deal closed.
CREATE OR REPLACE TABLE marts.cycle_time_by_month AS
SELECT
    CAST(date_trunc('month', close_date) AS DATE)   AS month,
    COUNT(*)                                        AS deals,
    MEDIAN(days_won_to_invoice)                     AS median_days_won_to_invoice,
    MEDIAN(days_invoice_to_cash)                    AS median_days_invoice_to_cash,
    COUNT(fully_paid_date)                          AS deals_paid
FROM marts.fct_order_to_cash
GROUP BY ALL;
