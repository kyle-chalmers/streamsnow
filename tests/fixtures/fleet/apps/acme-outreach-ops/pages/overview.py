"""Overview page for Outreach Ops (fleet fixture, warehouse runtime)."""

import streamlit as st

from branding import branded_metric
from session import get_session
from sql_loader import render_sql


@st.cache_data(ttl=1800)
def load_attempts(segment_expr: str, start_date: str, end_date: str):
    sql = render_sql("attempts", SEGMENT_EXPR=segment_expr)
    return get_session().sql(sql, params=[start_date, end_date]).to_pandas()


st.title("Outreach Ops")
st.caption("Dial attempts by weekday upload cap.")
st.subheader("Attempts")
st.caption("Attempts within the weekday upload window.")
branded_metric("Attempts", "12,480")
st.divider()
st.caption("Sources: ANALYTICS_DB.REPORTING.VW_OUTREACH_ATTEMPTS")
