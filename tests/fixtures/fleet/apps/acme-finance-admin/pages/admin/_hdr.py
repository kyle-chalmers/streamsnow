"""Shared header for the admin page group (imported package-qualified)."""

import streamlit as st


def header(title: str, caption: str) -> None:
    st.title(title)
    st.caption(caption)
