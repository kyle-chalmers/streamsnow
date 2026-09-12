-- Query: fees_by_partner
-- Feeds: Admin › Fee report
-- Schemas: ANALYTICS_DB.REPORTING
-- Params: :1 start_date, :2 end_date
SELECT
    partner_name,
    SUM(fee_amount)     AS fees
FROM ANALYTICS_DB.REPORTING.VW_FEES_BY_PARTNER
WHERE fee_date BETWEEN :1 AND :2
GROUP BY partner_name
ORDER BY fees DESC
