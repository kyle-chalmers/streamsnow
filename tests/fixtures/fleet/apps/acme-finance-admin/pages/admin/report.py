"""Admin report page (fleet fixture): nested package import shape."""

import streamlit as st

from branding import branded_metric
from pages.admin._hdr import header
from sql_loader import load_sql


@st.cache_data(ttl=1800)
def load_fees(start_date: str, end_date: str):
    conn = st.connection("snowflake")
    return conn.query(load_sql("fees_by_partner"), params=[start_date, end_date], ttl=0)


header("Finance Admin", "Fee reconciliation by partner.")
st.subheader("Fees")
st.caption("Fees collected per partner in the selected window.")
branded_metric("Fees", "$1,204,330.12")
st.divider()
st.caption("Sources: ANALYTICS_DB.REPORTING.VW_FEES_BY_PARTNER")
