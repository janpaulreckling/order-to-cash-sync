-- Data-quality control: does every won deal exist in the ERP with the same amount,
-- and does Salesforce show the status the ERP implies?
CREATE OR REPLACE TABLE marts.crm_erp_reconciliation AS
WITH open_dlq AS (
    SELECT source_id, string_agg(stage || ': ' || error, ' | ') AS dlq_reason
    FROM raw.dead_letters
    WHERE resolved_at IS NULL
    GROUP BY source_id
)
SELECT
    f.opportunity_id,
    f.opportunity_name,
    f.customer,
    f.close_date,
    f.crm_amount,
    f.order_name,
    f.order_amount,
    f.crm_invoice_status,
    f.erp_status,
    d.dlq_reason,
    CASE
        WHEN f.order_name IS NULL                            THEN 'missing in ERP'
        WHEN abs(f.crm_amount - f.order_amount) > 0.01       THEN 'amount mismatch'
        WHEN f.crm_invoice_status IS DISTINCT FROM f.erp_status THEN 'status not synced'
        ELSE 'ok'
    END AS check_status
FROM marts.fct_order_to_cash f
LEFT JOIN open_dlq d ON d.source_id = f.opportunity_id;
