"""Tests for the pre-publish privacy/export gate.

No organization-specific term appears in this file or in the scanner: the
org denylist is a gitignored local file, so the fixtures below build their own
throwaway denylist in ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

from streamsnow.tools import check_export_clean
from streamsnow.tools.check_export_clean import LOCAL_DENYLIST, load_denylist, main, scan_tree

REPO_ROOT = Path(__file__).resolve().parent.parent


def _denylist(root: Path, body: str) -> Path:
    path = root / LOCAL_DENYLIST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_clean_tree_passes(tmp_path):
    (tmp_path / "a.md").write_text("A generic Streamlit + Snowflake toolkit. Query ANALYTICS.")
    assert scan_tree(tmp_path)["ok"]


def test_local_denylist_term_is_detected_case_insensitively(tmp_path):
    _denylist(tmp_path, "# org names\n\nacme-internal-corp\n")
    (tmp_path / "b.md").write_text("This job reads from ACME-Internal-Corp.\n")
    res = scan_tree(tmp_path)
    assert not res["ok"]
    assert [f["match"] for f in res["findings"]] == ["acme-internal-corp"]


def test_local_denylist_regex_lines(tmp_path):
    _denylist(tmp_path, "re:\\bTKT-\\d{2,}\\b\n")
    ticket = "TKT" + "-1339"
    (tmp_path / "c.py").write_text(f"# tracked in {ticket}\n")
    res = scan_tree(tmp_path)
    assert not res["ok"]
    assert res["findings"][0]["match"] == ticket


def test_denylist_file_itself_is_never_a_finding(tmp_path):
    _denylist(tmp_path, "acme-internal-corp\n")
    assert scan_tree(tmp_path)["ok"]


def test_without_local_denylist_only_generic_checks_run(tmp_path):
    (tmp_path / "b.md").write_text("This job reads from acme-internal-corp.\n")
    res = scan_tree(tmp_path)
    assert res["ok"] and res["denylist_terms"] == 0


def test_load_denylist_skips_blanks_and_comments(tmp_path):
    path = _denylist(tmp_path, "# comment\n\n  Foo Bar  \nre:baz\\d+\n")
    terms, patterns = load_denylist(path)
    assert terms == ["foo bar"]
    assert [p.pattern for p in patterns] == ["baz\\d+"]
    assert load_denylist(tmp_path / "missing.txt") == ([], [])


def test_explicit_denylist_path(tmp_path, capsys):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("acme-internal-corp\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.md").write_text("acme-internal-corp\n")
    assert main([str(repo), "--denylist", str(outside)]) == 1
    assert main([str(repo), "--denylist", str(tmp_path / "nope.txt")]) == 2


def test_detects_personal_path(tmp_path):
    (tmp_path / "c.py").write_text("P = '/Users/someone/secret/x'\n")
    assert not scan_tree(tmp_path)["ok"]


def test_detects_private_key_block(tmp_path):
    (tmp_path / "k.txt").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nabcd\n-----END RSA PRIVATE KEY-----\n"
    )
    assert not scan_tree(tmp_path)["ok"]


def test_detects_real_email_but_allows_reserved_domains(tmp_path):
    (tmp_path / "ok.md").write_text(
        "a@example.com b@acme.example c@corp.test d@example.org\n"
        "@st.cache_data\ndef f(): ...\nfn@st.cache_data\n"
    )
    assert scan_tree(tmp_path)["ok"]
    (tmp_path / "bad.md").write_text("contact jane.doe@realcorp.io for access\n")
    res = scan_tree(tmp_path)
    assert not res["ok"]
    assert res["findings"][0]["match"] == "jane.doe@realcorp.io"


def test_scanner_source_names_no_organization():
    """The committed scanner holds generic patterns only. Org terms live in the
    gitignored local denylist; the gitignore entry is what keeps it local."""
    src = Path(check_export_clean.__file__).read_text()
    assert "DENY_TERMS" not in src
    assert str(LOCAL_DENYLIST) in (REPO_ROOT / ".gitignore").read_text()


def test_repo_tree_is_clean_under_generic_checks():
    """The published tree itself passes (the CI privacy-gate job, as a test)."""
    res = scan_tree(REPO_ROOT, denylist=REPO_ROOT / "does-not-exist.txt")
    assert res["ok"], res["findings"]


def test_load_denylist_skips_malformed_regex_instead_of_crashing(tmp_path, capsys):
    path = _denylist(tmp_path, "good term\nre:(unclosed\nre:ok\\d+\n")
    terms, patterns = load_denylist(path)
    assert terms == ["good term"]
    assert [p.pattern for p in patterns] == ["ok\\d+"]
    assert "unclosed" in capsys.readouterr().err
