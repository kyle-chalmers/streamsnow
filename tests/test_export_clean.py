"""Tests for the pre-publish privacy/export gate."""

from __future__ import annotations

from streamsnow.tools.check_export_clean import scan_tree


def test_clean_tree_passes(tmp_path):
    (tmp_path / "a.md").write_text("A generic Streamlit + Snowflake toolkit. Query ANALYTICS.")
    assert scan_tree(tmp_path)["ok"]


def test_detects_proprietary_term(tmp_path):
    (tmp_path / "b.md").write_text("This job reads from initech.")
    res = scan_tree(tmp_path)
    assert not res["ok"]
    assert any("initech" in f["match"] for f in res["findings"])


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


def test_detects_source_org_names(tmp_path):
    # Case-folded organization terms: a pasted internal reference must fail
    # the export gate. Terms are spelled only in this exempt file.
    for leak in ("built at HappyMoney", "Happy Money internal", "the happy-money org"):
        doc = tmp_path / "doc.md"
        doc.write_text(f"prose. {leak}. prose.\n")
        assert not scan_tree(tmp_path)["ok"], leak


def test_detects_lending_domain_vocabulary(tmp_path):
    # The OSS release must carry zero source-domain flavor — vocabulary, not
    # just literals (e.g. a fixture describing a "borrower payoff" scenario).
    (tmp_path / "doc.md").write_text("example table of borrower payoff events\n")
    assert not scan_tree(tmp_path)["ok"]
