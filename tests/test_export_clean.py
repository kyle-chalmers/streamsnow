"""Tests for the pre-publish privacy/export gate."""

from __future__ import annotations

from streamsnow.tools.check_export_clean import scan_tree


def test_clean_tree_passes(tmp_path):
    (tmp_path / "a.md").write_text("A generic Streamlit + Snowflake toolkit. Query ANALYTICS.")
    assert scan_tree(tmp_path)["ok"]


def test_detects_proprietary_term(tmp_path):
    (tmp_path / "b.md").write_text("This job reads from loanpro.")
    res = scan_tree(tmp_path)
    assert not res["ok"]
    assert any("loanpro" in f["match"] for f in res["findings"])


def test_detects_ticket_prefix_and_personal_path(tmp_path):
    (tmp_path / "c.py").write_text("# tracked in DI-1339\nP = '/Users/someone/secret/x'\n")
    res = scan_tree(tmp_path)
    matches = {f["match"] for f in res["findings"]}
    assert not res["ok"]
    assert any(m.startswith("DI-") for m in matches)


def test_detects_private_key_block(tmp_path):
    (tmp_path / "k.txt").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nabcd\n-----END RSA PRIVATE KEY-----\n"
    )
    assert not scan_tree(tmp_path)["ok"]
