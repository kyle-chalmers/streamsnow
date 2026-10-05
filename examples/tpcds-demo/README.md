# TPC-DS demo data

A small, realistic sales dataset for trying StreamSnow end to end. `extract.sql` copies part of
Snowflake's built-in TPC-DS sample data into your own database, so the governed repo has a real
schema to point at.

## What it builds

`STREAMSNOW_DEMO.TPCDS`, four tables:

| Table | Grain | Rows |
|---|---|---|
| `STORE_SALES` | one row per receipt line (ticket number + item), 3 stores, 2001-01-01 to 2002-12-30 | 42,698,452 |
| `DATE_DIM` | one row per calendar day | 73,049 |
| `STORE` | one row per store record, the same 3 stores | 3 |
| `ITEM` | one row per item record | 402,000 |

Two full years give real seasonality (sales rise from August and peak in November and December)
and a year-over-year comparison. The dates are historical, so dashboards and review windows should
anchor to the latest date in the data rather than today.

## How to run it

1. Replace `<your_dev_role>` with the role your `snow` connection uses.
2. Run the file in a Snowsight worksheet as `SYSADMIN` (it creates a database, a schema, and a
   temporary warehouse, `STREAMSNOW_EXTRACT_WH`, that it drops at the end). Its `CREATE WAREHOUSE`
   fails if a warehouse with that name already exists, so the script never drops one it did not
   create. If an earlier run stopped partway, drop the leftover warehouse and run it again.
3. Point the setup wizard at database `STREAMSNOW_DEMO` and schema `TPCDS`.

## Cost

Measured on 2026-09-26: the `STORE_SALES` build scans about 482 GB after date pruning and runs in
about 19 seconds on a LARGE warehouse. The whole script billed roughly 0.13 to 0.16 credits, most
of it the one-minute minimum. Your number depends on your edition and region pricing.

## Using your own data instead

Any small table you already have works. Answer the wizard's database and schema questions with
where that table lives, and describe the dashboard you want to `/build-app`. Keep the first app
small: one table, a few visuals, a date range anchored to the data.
