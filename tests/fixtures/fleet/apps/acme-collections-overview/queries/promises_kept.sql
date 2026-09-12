-- Query: promises_kept
-- Feeds: Overview page (promise-kept rate)
-- Schemas: ANALYTICS_DB.REPORTING
-- Params: :1 start_date, :2 end_date
SELECT
    promise_date                 AS dt,
    SUM(kept_promises)           AS kept,
    SUM(promises_due)            AS due
FROM ANALYTICS_DB.REPORTING.VW_PROMISES_DAILY
WHERE promise_date BETWEEN :1 AND :2
GROUP BY promise_date
ORDER BY promise_date
