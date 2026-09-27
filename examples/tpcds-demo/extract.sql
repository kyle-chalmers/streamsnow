-- StreamSnow demo data: a small TPC-DS store-sales extract
-- Source: SNOWFLAKE_SAMPLE_DATA.TPCDS_SF10TCL (shared sample database, read-only)
-- Target: STREAMSNOW_DEMO.TPCDS
-- Run as: SYSADMIN (creates a database, a schema and a temporary warehouse)
--
-- What it builds (grain in brackets):
--   STORE_SALES  [one row per receipt line: ticket number + item]  3 stores, 2001-01-01 to 2002-12-30 (42,698,452 rows)
--   DATE_DIM     [one row per calendar day]                          full TPC-DS calendar (73,049 rows)
--   STORE        [one row per store record]                           the 3 stores above
--   ITEM         [one row per item record]                            full item table (402,000 rows)
-- Why these filters: two full years gives real seasonality (Q4 peaks) and a year-over-year comparison,
-- and three stores keeps the fact table near 43M rows so dashboard queries stay fast on an XSMALL.
-- Store keys 1, 7 and 13 are used because TPC-DS sales in this period reference them (keys 3 and 9 have none).
-- The sales dates are historical (TPC-DS ends in early 2003), so review windows anchor to MAX(SOLD_DATE).
--
-- Cost (measured 2026-09-26): the STORE_SALES build scans about 482 GB after date pruning and runs in
-- about 19 seconds on a LARGE warehouse; the whole script bills roughly 0.13 to 0.16 credits (one
-- minute minimum on LARGE). The date filter prunes STORE_SALES by its clustering key (SS_SOLD_DATE_SK).

USE ROLE SYSADMIN;
-- Plain CREATE (no IF NOT EXISTS) on purpose: the last line drops this warehouse, so the script
-- must fail here if the name is already taken rather than adopt and later drop a warehouse it did
-- not create. If an earlier run of this script stopped before the end, drop its leftover
-- STREAMSNOW_EXTRACT_WH yourself first.
CREATE WAREHOUSE STREAMSNOW_EXTRACT_WH
  WAREHOUSE_SIZE = LARGE AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE
  COMMENT = 'One-time TPC-DS extract for the StreamSnow demo; dropped at the end of this script';
USE WAREHOUSE STREAMSNOW_EXTRACT_WH;
ALTER SESSION SET QUERY_TAG = 'streamsnow_tpcds_extract';

CREATE DATABASE IF NOT EXISTS STREAMSNOW_DEMO COMMENT = 'StreamSnow demo data (TPC-DS extract)';
CREATE SCHEMA IF NOT EXISTS STREAMSNOW_DEMO.TPCDS;

-- d_date_sk 2451911 = 2001-01-01, 2452639 = 2002-12-31
CREATE OR REPLACE TABLE STREAMSNOW_DEMO.TPCDS.STORE_SALES AS
SELECT
    d.d_date                 AS sold_date,
    ss.ss_sold_date_sk       AS sold_date_sk,
    ss.ss_store_sk           AS store_sk,
    ss.ss_item_sk            AS item_sk,
    ss.ss_ticket_number      AS ticket_number,
    ss.ss_quantity           AS quantity,
    ss.ss_sales_price        AS sales_price,
    ss.ss_ext_sales_price    AS ext_sales_price,
    ss.ss_net_paid           AS net_paid,
    ss.ss_net_profit         AS net_profit
FROM SNOWFLAKE_SAMPLE_DATA.TPCDS_SF10TCL.STORE_SALES ss
JOIN SNOWFLAKE_SAMPLE_DATA.TPCDS_SF10TCL.DATE_DIM d
  ON d.d_date_sk = ss.ss_sold_date_sk
WHERE ss.ss_sold_date_sk BETWEEN 2451911 AND 2452639
  AND ss.ss_store_sk IN (1, 7, 13);

CREATE OR REPLACE TABLE STREAMSNOW_DEMO.TPCDS.DATE_DIM AS
SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCDS_SF10TCL.DATE_DIM;

CREATE OR REPLACE TABLE STREAMSNOW_DEMO.TPCDS.STORE AS
SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCDS_SF10TCL.STORE
WHERE s_store_sk IN (1, 7, 13);

CREATE OR REPLACE TABLE STREAMSNOW_DEMO.TPCDS.ITEM AS
SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCDS_SF10TCL.ITEM;

-- Let your development role (the one your snow connection uses) read the extract for local preview.
-- Replace <your_dev_role> before running. The deploy role's read grant comes from `streamsnow deploy-setup --admin`.
GRANT USAGE ON DATABASE STREAMSNOW_DEMO TO ROLE <your_dev_role>;
GRANT USAGE ON SCHEMA STREAMSNOW_DEMO.TPCDS TO ROLE <your_dev_role>;
GRANT SELECT ON ALL TABLES IN SCHEMA STREAMSNOW_DEMO.TPCDS TO ROLE <your_dev_role>;

-- Row counts (expected: STORE_SALES 42,698,452; DATE_DIM 73,049; STORE 3; ITEM 402,000).
SELECT 'STORE_SALES' t, COUNT(*) n, MIN(sold_date) min_d, MAX(sold_date) max_d FROM STREAMSNOW_DEMO.TPCDS.STORE_SALES
UNION ALL SELECT 'DATE_DIM', COUNT(*), NULL, NULL FROM STREAMSNOW_DEMO.TPCDS.DATE_DIM
UNION ALL SELECT 'STORE', COUNT(*), NULL, NULL FROM STREAMSNOW_DEMO.TPCDS.STORE
UNION ALL SELECT 'ITEM', COUNT(*), NULL, NULL FROM STREAMSNOW_DEMO.TPCDS.ITEM;

ALTER SESSION UNSET QUERY_TAG;
DROP WAREHOUSE IF EXISTS STREAMSNOW_EXTRACT_WH;
