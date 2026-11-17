-- Open receivables by days past due, as of today. Every bucket is present, even when empty.
CREATE OR REPLACE TABLE marts.ar_aging AS
WITH buckets(bucket_order, bucket, low, high) AS (
    VALUES (1, 'Not yet due', -100000, 0), (2, '1-30 days', 1, 30), (3, '31-60 days', 31, 60),
           (4, '61-90 days', 61, 90), (5, '90+ days', 91, 100000)
),
open_items AS (
    SELECT document_no, customer, amount_residual,
           date_diff('day', due_date, current_date) AS days_past_due
    FROM staging.invoices
    WHERE move_type = 'out_invoice' AND amount_residual > 0
)
SELECT
    b.bucket_order,
    b.bucket,
    COUNT(i.document_no)                  AS invoices,
    COALESCE(SUM(i.amount_residual), 0)   AS amount_open
FROM buckets b
LEFT JOIN open_items i ON i.days_past_due BETWEEN b.low AND b.high
GROUP BY ALL;
