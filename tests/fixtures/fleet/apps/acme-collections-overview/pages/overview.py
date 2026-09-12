"""Overview page for Collections Overview (fleet fixture, container runtime)."""

import plotly.express as px
import streamlit as st

from branding import BRAND_CHART_COLORS, branded_metric
from pages._glossary import metric_help, render_glossary
from sql_loader import load_sql


@st.cache_data(ttl=1800)
def load_promises(start_date: str, end_date: str):
    sql = load_sql("promises_kept")
    conn = st.connection("snowflake")
    return conn.query(sql, params=[start_date, end_date], ttl=0)


st.title("Collections Overview")
st.caption("Promise-kept performance by day.")

st.subheader("Promise-kept rate")
st.caption("Kept promises over promises due, at the daily grain.")
branded_metric("Promise-kept rate", "82.4%", delta="+1.1%")
st.caption(metric_help("promise_kept_rate"))

fig = px.bar(x=["Mon", "Tue"], y=[3, 1], color_discrete_sequence=BRAND_CHART_COLORS)
st.plotly_chart(fig, use_container_width=True)
render_glossary("promise_kept_rate")

st.divider()
st.caption("Sources: ANALYTICS_DB.REPORTING.VW_PROMISES_DAILY")
