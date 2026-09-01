"""Tests for the personal-absolute-path (origin leak) check.

Fixture paths are assembled with f-strings on purpose: this repo's own
export-clean gate flags literal ``/Users/<name>/`` and ``/home/<name>/``
strings in source files, and the ``{u}`` interpolation keeps the leaky literal
out of this file while producing it at runtime for the tool under test.
"""

from __future__ import annotations

from pathlib import Path

from streamsnow.tools import check_path_leaks

U = "jane"  # a fictional developer username, interpolated into fixture paths


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_windows_user_dir_flagged_both_separators(tmp_path):
    p = _write(
        tmp_path / "AGENTS.md",
        "Built from C:/Users/Jane Doe/Development/acme-sales-dashboard/\n"
        "and C:\\Users\\jdoe\\repos\\tool.py\n",
    )
    res = check_path_leaks.scan_paths([p])
    assert not res["ok"]
    assert [f["line"] for f in res["findings"]] == [1, 2]
    assert all("Windows user dir" in f["detail"] for f in res["findings"])


def test_mac_dev_dir_flagged_but_generic_mac_home_allowed(tmp_path):
    bad = _write(tmp_path / "notes.md", f"see /Users/{U}/Development/acme-app/branding.py\n")
    assert not check_path_leaks.scan_paths([bad])["ok"]
    # Generic Mac home (no /Development/) is legitimate in docs — too noisy to block.
    ok = _write(tmp_path / "docs.md", f"config lives at /Users/{U}/.snowflake/config.toml\n")
    assert check_path_leaks.scan_paths([ok])["ok"]


def test_linux_home_flagged_but_actions_runner_allowed(tmp_path):
    bad = _write(tmp_path / "helper.py", f"# leftover: /home/{U}/scripts/refresh.sh\n")
    res = check_path_leaks.scan_paths([bad])
    assert not res["ok"]
    assert "Linux user home" in res["findings"][0]["detail"]
    runner = "runner"
    ok = _write(tmp_path / "ci.md", f"Actions checkouts land under /home/{runner}/work/\n")
    assert check_path_leaks.scan_paths([ok])["ok"]


def test_placeholder_usernames_never_flagged(tmp_path):
    p = _write(
        tmp_path / "README.md",
        "Use /Users/<user>/Development/ or C:/Users/{user}/ as a placeholder.\n",
    )
    assert check_path_leaks.scan_paths([p])["ok"]


def test_bare_prefix_mention_not_flagged(tmp_path):
    # No trailing separator after the username -> not a path-like context.
    p = _write(tmp_path / "a.md", "Windows keeps profiles under C:\\Users\n")
    assert check_path_leaks.scan_paths([p])["ok"]


def test_only_py_and_md_scanned(tmp_path):
    _write(tmp_path / "app" / "log.txt", f"/Users/{U}/Development/x\n")
    _write(tmp_path / "app" / "q.sql", f"-- /home/{U}/x/\n")
    assert check_path_leaks.scan_paths([tmp_path])["ok"]


def test_dotted_dirs_below_root_skipped_but_dotted_root_scanned(tmp_path):
    _write(tmp_path / "apps" / ".review" / "report.md", f"/Users/{U}/Development/x\n")
    assert check_path_leaks.scan_paths([tmp_path / "apps"])["ok"]
    # An explicitly-passed dotted root is still scanned.
    skills = _write(tmp_path / ".skills" / "recipe.md", f"run /home/{U}/bin/tool\n")
    assert not check_path_leaks.scan_paths([skills.parent])["ok"]


def test_missing_path_skipped_silently(tmp_path):
    assert check_path_leaks.scan_paths([tmp_path / "deleted.py"])["ok"]


def test_main_exit_codes_and_json(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "apps" / "acme-sales-dashboard" / "pages" / "trends.py", "x = 1\n")
    assert check_path_leaks.main([]) == 0  # default path: apps
    assert "clean" in capsys.readouterr().out

    _write(
        tmp_path / "apps" / "acme-sales-dashboard" / "AGENTS.md",
        f"screenshots in /Users/{U}/Development/shots/\n",
    )
    assert check_path_leaks.main(["--format", "json"]) == 1
    import json

    payload = json.loads(capsys.readouterr().out)
    assert not payload["ok"]
    assert payload["findings"][0]["line"] == 1
