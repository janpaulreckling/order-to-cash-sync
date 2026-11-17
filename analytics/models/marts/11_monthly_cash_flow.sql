-- Bookings (deals won), billings (net invoiced) and collections (cash in) per month.
CREATE OR REPLACE TABLE marts.monthly_cash_flow AS
WITH months AS (
    SELECT CAST(m AS DATE) AS month
    FROM range(
        (SELECT date_trunc('month', MIN(close_date)) FROM staging.opportunities WHERE is_won),
        date_trunc('month', current_date) + INTERVAL 1 MONTH,
        INTERVAL 1 MONTH) t(m)
)
SELECT
    m.month,
    COALESCE((SELECT SUM(amount) FROM staging.opportunities
              WHERE is_won AND date_trunc('month', close_date) = m.month), 0)  AS bookings,
    COALESCE((SELECT SUM(sign * amount_total) FROM staging.invoices
              WHERE date_trunc('month', invoice_date) = m.month), 0)          AS billings,
    COALESCE((SELECT SUM(amount) FROM staging.payments
              WHERE date_trunc('month', payment_date) = m.month), 0)          AS collections
FROM months m;
