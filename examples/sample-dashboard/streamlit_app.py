"""StreamSnow example dashboard — entrypoint.

A runnable demo of what `streamsnow init` scaffolds, but wired to deterministic
sample data so it renders anywhere with `streamlit run` and NO Snowflake
connection. The shape mirrors a real StreamSnow app:

    streamlit_app.py   # st.navigation entrypoint + apply_branding()
    branding.py        # brand colors, Plotly template, branded_metric card
    data.py            # cached loaders (here: mock data; in a real app: conn.query)
    pages/             # one module per page

Run it:  streamlit run examples/sample-dashboard/streamlit_app.py
"""

import streamlit as st

st.set_page_config(page_title="StreamSnow Example", page_icon="📊", layout="wide")

from branding import apply_branding  # noqa: E402  (must follow set_page_config)

apply_branding()

nav = st.navigation(
    [
        st.Page("pages/overview.py", title="Overview", icon="📈", default=True),
        st.Page("pages/trends.py", title="Trends", icon="📊"),
    ]
)
nav.run()
