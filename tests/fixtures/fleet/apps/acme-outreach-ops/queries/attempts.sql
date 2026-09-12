-- Query: attempts
-- Feeds: Overview page (attempts)
-- Schemas: ANALYTICS_DB.REPORTING
-- Params: :1 start_date, :2 end_date
-- Tokens: SEGMENT_EXPR (segment filter fragment, written without braces here on purpose)
SELECT
    attempt_date        AS dt,
    COUNT(*)            AS attempts
FROM ANALYTICS_DB.REPORTING.VW_OUTREACH_ATTEMPTS
WHERE attempt_date BETWEEN :1 AND :2
  {SEGMENT_EXPR}
GROUP BY attempt_date
ORDER BY attempt_date
