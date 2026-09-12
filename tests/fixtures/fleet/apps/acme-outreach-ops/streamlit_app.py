"""Acme Outreach Ops — entrypoint. Scaffolded by StreamSnow (warehouse runtime)."""

import streamlit as st

st.set_page_config(page_title="Acme Outreach Ops", page_icon="📊", layout="wide")

from branding import apply_branding  # noqa: E402  (must follow set_page_config)

apply_branding()

nav = st.navigation(
    [
        st.Page("pages/overview.py", title="Overview", icon="📈", default=True),
    ]
)
nav.run()
