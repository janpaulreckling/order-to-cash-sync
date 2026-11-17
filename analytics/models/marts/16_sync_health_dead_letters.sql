-- Open dead letters with the business context a reviewer needs.
CREATE OR REPLACE TABLE marts.sync_health_dead_letters AS
SELECT
    d.stage,
    o.opportunity_name,
    o.customer,
    o.amount            AS crm_amount,
    d.error,
    d.attempts,
    CAST(d.created_at AS DATE)      AS first_seen,
    CAST(d.last_seen_at AS DATE)    AS last_seen
FROM raw.dead_letters d
LEFT JOIN staging.opportunities o ON o.opportunity_id = d.source_id
WHERE d.resolved_at IS NULL
ORDER BY d.created_at;
