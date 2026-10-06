"""Tests for the per-app stage-copy deploy bundle (streamsnow/stage_bundle.py).

The stage-copy workflow used to upload the whole ``apps/`` tree, so every
deploy shipped each app's AGENTS.md, REQUIREMENTS.md and ``sql_review/`` audit
trail to the stage. The bundle keeps what the running app reads and drops the
rest. All fixtures are ``tmp_path`` Acme apps; nothing touches Snowflake.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from streamsnow.cli import app as cli_app
from streamsnow.config import Config
from streamsnow.scaffolder import scaffold
from streamsnow.stage_bundle import BundleError, build_bundle, excluded_reason, select_app_files
from streamsnow.tools.check_artifacts import _deployable_files

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"
FLEET = REPO_ROOT / "tests" / "fixtures" / "fleet"
SLUG = "acme-sales"


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    cfg = Config.from_dict(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    scaffold(cfg, repo, SLUG)
    return repo


def _shipped(out: Path, slug: str = SLUG) -> set[str]:
    root = out / slug
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _declare(app_dir: Path, *entries: str) -> None:
    yml = app_dir / "snowflake.yml"
    data = yaml.safe_load(yml.read_text(encoding="utf-8"))
    (entity,) = data["entities"].values()
    entity["artifacts"].extend(entries)
    yml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ---- excluded_reason ---------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "AGENTS.md",
        "REQUIREMENTS.md",
        "CLAUDE.md",
        "README.md",
        "sql_review/index.yaml",
        "sql_review/review_log/2026-10-01.md",
        ".git/config",
        "pages/.cache/x.py",
        "__pycache__/streamlit_app.cpython-311.pyc",
        "pages/__pycache__/overview.cpython-311.pyc",
        ".streamlit/secrets.toml",
        ".streamlit/secrets.toml.example",
    ],
)
def test_excluded_reason_names_why_a_path_never_ships(rel):
    assert excluded_reason(rel, ["streamlit_app.py", "pages/"])


@pytest.mark.parametrize(
    "rel",
    [
        "streamlit_app.py",
        "snowflake.yml",
        "pages/overview.py",
        "pages/notes.md",  # only root-level docs are internal
        "queries/example_metric.sql",
        ".streamlit/config.toml",
        "pyproject.toml",
        "assets/logo.png",
    ],
)
def test_excluded_reason_keeps_runtime_files(rel):
    assert excluded_reason(rel, ["streamlit_app.py", "pages/"]) is None


def test_excluded_reason_keeps_a_root_doc_an_artifacts_entry_declares():
    assert excluded_reason("help.md", ["streamlit_app.py", "help.md"]) is None
    assert excluded_reason("help.md", ["streamlit_app.py", "*.md"]) is None
    assert excluded_reason("help.md", ["streamlit_app.py"])
    assert excluded_reason("help.md", None)  # no artifacts list declares nothing


def test_excluded_reason_never_ships_secrets_even_when_declared():
    assert excluded_reason(".streamlit/secrets.toml", [".streamlit/"])
    assert excluded_reason("sql_review/index.yaml", ["sql_review/"])


# ---- build_bundle ------------------------------------------------------------


def test_bundle_excludes_internal_docs_and_sql_review(tmp_path):
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    _write(app_dir / "REQUIREMENTS.md", "# Requirements\n")
    _write(app_dir / "CLAUDE.md", "@AGENTS.md\n")
    _write(app_dir / "sql_review" / "review_log" / "2026-10-01.md", "signed off\n")
    out = tmp_path / "bundle"

    result = build_bundle(repo, out)

    shipped = _shipped(out)
    assert not {"AGENTS.md", "REQUIREMENTS.md", "CLAUDE.md"} & shipped
    assert not any(p.startswith("sql_review/") for p in shipped)
    for keep in ("streamlit_app.py", "snowflake.yml", "pages/overview.py", "pyproject.toml"):
        assert keep in shipped, (keep, shipped)
    (entry,) = result["apps"]
    assert entry["slug"] == SLUG
    excluded = {e["path"]: e["reason"] for e in entry["excluded"]}
    assert "AGENTS.md" in excluded and "sql_review/review_log/2026-10-01.md" in excluded
    assert all("\\" not in p for p in entry["files"])  # as_posix output on every OS


def test_bundle_keeps_a_declared_doc(tmp_path):
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    _write(app_dir / "help.md", "# How to read this dashboard\n")
    _declare(app_dir, "help.md")
    out = tmp_path / "bundle"

    build_bundle(repo, out)

    assert "help.md" in _shipped(out)
    assert "AGENTS.md" not in _shipped(out)


def test_bundle_ships_config_toml_but_never_secrets(tmp_path):
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    _write(app_dir / ".streamlit" / "secrets.toml", 'password = "acme-not-real"\n')
    out = tmp_path / "bundle"

    build_bundle(repo, out)

    shipped = _shipped(out)
    assert ".streamlit/config.toml" in shipped
    assert ".streamlit/secrets.toml" not in shipped
    assert ".streamlit/secrets.toml.example" not in shipped


@pytest.mark.parametrize("rel", [".env", ".env.local", "pages/.env", "queries/.env.production"])
def test_excluded_reason_never_ships_an_env_file(rel):
    reason = excluded_reason(rel, [rel, "pages/", "queries/"])  # even when declared
    assert reason == "environment file (may hold secrets)"


@pytest.mark.parametrize("rel", ["environment.yml", "pages/env_helpers.py", ".envrc_notes.py"])
def test_excluded_reason_keeps_files_that_only_look_like_env(rel):
    assert excluded_reason(rel, None) is None


def test_bundle_never_ships_env_files_but_keeps_config_toml(tmp_path):
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    for rel in (".env", ".env.local", "pages/.env", "pages/.env.local"):
        _write(app_dir / rel, "SNOWFLAKE_PASSWORD=acme-not-real\n")
    out = tmp_path / "bundle"

    result = build_bundle(repo, out)

    shipped = _shipped(out)
    assert not any(p.rsplit("/", 1)[-1].startswith(".env") for p in shipped), shipped
    assert ".streamlit/config.toml" in shipped
    reasons = {e["path"]: e["reason"] for e in result["apps"][0]["excluded"]}
    for rel in (".env", ".env.local", "pages/.env", "pages/.env.local"):
        assert reasons[rel] == "environment file (may hold secrets)"


def test_bundle_refuses_a_dangling_symlink_out(tmp_path):
    repo = _repo(tmp_path)
    out = tmp_path / "bundle"
    try:
        out.symlink_to(tmp_path / "gone")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(BundleError, match="symlink"):
        build_bundle(repo, out)
    res = _invoke("--out", str(out), "--dir", str(repo))
    assert res.exit_code == 2, res.output


def test_bundle_skips_tooling_dirs(tmp_path):
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    _write(app_dir / "__pycache__" / "branding.cpython-311.pyc", "x")
    _write(app_dir / ".pytest_cache" / "README.md", "x")
    out = tmp_path / "bundle"

    build_bundle(repo, out)

    shipped = _shipped(out)
    assert not any("__pycache__" in p or ".pytest_cache" in p for p in shipped)


def test_bundle_skips_a_symlink_that_escapes_the_app(tmp_path):
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    outside = _write(tmp_path / "outside" / "private_notes.py", "TOKEN = 'acme'\n")
    inside = app_dir / "shared_helpers.py"
    _write(app_dir / "helpers" / "real.py", "X = 1\n")
    try:
        (app_dir / "leak.py").symlink_to(outside)
        inside.symlink_to(app_dir / "helpers" / "real.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    out = tmp_path / "bundle"

    result = build_bundle(repo, out)

    shipped = _shipped(out)
    assert "leak.py" not in shipped
    assert "shared_helpers.py" in shipped  # a link inside the app ships its content
    reasons = {e["path"]: e["reason"] for e in result["apps"][0]["excluded"]}
    assert "escapes" in reasons["leak.py"]


def _link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")


def _reasons(result: dict) -> dict[str, str]:
    return {e["path"]: e["reason"] for e in result["apps"][0]["excluded"]}


@pytest.mark.parametrize(
    ("target", "alias"),
    [
        (".env", "runtime.txt"),
        ("AGENTS.md", "notes.txt"),
        ("sql_review/index.yaml", "review.yaml"),
        (".streamlit/secrets.toml", "pages/settings.toml"),
    ],
)
def test_bundle_excludes_a_symlink_alias_of_an_excluded_file(tmp_path, target, alias):
    """Codex P1: the link's own name passed the rules, then copyfile followed it."""
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    if not (app_dir / target).exists():
        _write(app_dir / target, "SNOWFLAKE_PASSWORD=acme-not-real\n")
    _link(app_dir / alias, app_dir / target)
    out = tmp_path / "bundle"

    result = build_bundle(repo, out)

    assert alias not in _shipped(out)
    assert _reasons(result)[alias] == excluded_reason(target, ["streamlit_app.py"])


def test_bundle_excludes_files_reached_through_a_link_to_an_excluded_dir(tmp_path):
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    _link(app_dir / "review_copy", app_dir / "sql_review")
    out = tmp_path / "bundle"

    build_bundle(repo, out)

    assert not any(p.startswith("review_copy/") for p in _shipped(out))


def test_bundle_follows_a_directory_symlink_inside_the_app(tmp_path):
    """Codex P2: `.streamlit -> config/` shipped config/config.toml but not
    .streamlit/config.toml, so the deployed app lost its theme."""
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    shutil.rmtree(app_dir / ".streamlit")
    _write(app_dir / "config" / "config.toml", "[theme]\nbase = 'light'\n")
    _link(app_dir / ".streamlit", app_dir / "config")
    _write(app_dir / "helpers" / "real.py", "X = 1\n")
    _link(app_dir / "lib", app_dir / "helpers")
    out = tmp_path / "bundle"

    build_bundle(repo, out)

    shipped = _shipped(out)
    assert ".streamlit/config.toml" in shipped
    assert "lib/real.py" in shipped and "helpers/real.py" in shipped


def test_bundle_link_path_rules_apply_inside_a_followed_directory(tmp_path):
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    shutil.rmtree(app_dir / ".streamlit")
    _write(app_dir / "settings" / "config.toml", "[theme]\nbase = 'light'\n")
    _write(app_dir / "settings" / ".env", "SNOWFLAKE_PASSWORD=acme-not-real\n")
    _link(app_dir / ".streamlit", app_dir / "settings")
    out = tmp_path / "bundle"

    result = build_bundle(repo, out)

    shipped = _shipped(out)
    assert ".streamlit/config.toml" in shipped
    assert ".streamlit/.env" not in shipped and "settings/.env" not in shipped
    assert ".streamlit/.env" in _reasons(result)


def test_bundle_survives_a_directory_symlink_cycle(tmp_path):
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    _link(app_dir / "pages" / "loop", app_dir)
    out = tmp_path / "bundle"

    result = build_bundle(repo, out)

    shipped = _shipped(out)
    assert "pages/overview.py" in shipped
    assert not any(p.startswith("pages/loop/") for p in shipped)
    assert "cycle" in _reasons(result)["pages/loop/"]


def test_bundle_excludes_a_directory_symlink_that_escapes_the_app(tmp_path):
    repo = _repo(tmp_path)
    app_dir = repo / "apps" / SLUG
    _write(tmp_path / "outside" / "vendor.py", "TOKEN = 'acme'\n")
    _link(app_dir / "vendor", tmp_path / "outside")
    out = tmp_path / "bundle"

    result = build_bundle(repo, out)

    assert not any(p.startswith("vendor/") for p in _shipped(out))
    assert "escapes" in _reasons(result)["vendor/"]


def test_select_app_files_matches_what_the_bundle_writes(tmp_path):
    repo = _repo(tmp_path)
    out = tmp_path / "bundle"
    result = build_bundle(repo, out)
    selected, excluded = select_app_files(repo / "apps" / SLUG)
    assert sorted(selected) == result["apps"][0]["files"] == sorted(_shipped(out))
    assert excluded == result["apps"][0]["excluded"]


def test_bundle_refuses_a_non_empty_out(tmp_path):
    repo = _repo(tmp_path)
    out = _write(tmp_path / "bundle" / "stale.txt", "old run\n").parent
    with pytest.raises(BundleError, match="not empty"):
        build_bundle(repo, out)


def test_bundle_refuses_an_out_inside_apps(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(BundleError, match="inside"):
        build_bundle(repo, repo / "apps" / "bundle")


def test_bundle_refuses_a_bad_slug(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(BundleError, match="slug"):
        build_bundle(repo, tmp_path / "bundle", ["../etc"])
    with pytest.raises(BundleError, match="no app directory"):
        build_bundle(repo, tmp_path / "bundle", ["acme-missing"])


def test_bundle_with_no_slugs_takes_every_app_dir(tmp_path):
    repo = _repo(tmp_path)
    shutil.copytree(repo / "apps" / SLUG, repo / "apps" / "acme-ops")
    out = tmp_path / "bundle"

    result = build_bundle(repo, out)

    assert [a["slug"] for a in result["apps"]] == ["acme-ops", SLUG]
    assert "streamlit_app.py" in _shipped(out, "acme-ops")


def test_every_fleet_app_keeps_each_deployable_file(tmp_path):
    out = tmp_path / "bundle"
    result = build_bundle(FLEET, out)
    assert result["apps"], "fleet fixture has no apps"
    for entry in result["apps"]:
        need = set(_deployable_files(FLEET / "apps" / entry["slug"]))
        assert need, entry["slug"]
        assert need <= _shipped(out, entry["slug"]), (entry["slug"], need - set(entry["files"]))
        assert "AGENTS.md" not in _shipped(out, entry["slug"])


# ---- CLI ----------------------------------------------------------------------


def _invoke(*args: str):
    return CliRunner().invoke(cli_app, ["stage-bundle", *args])


def test_cli_writes_the_bundle_and_reports_json(tmp_path):
    repo = _repo(tmp_path)
    out = tmp_path / "bundle"
    res = _invoke("--out", str(out), "--dir", str(repo), "--format", "json")
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["ok"] is True
    assert [a["slug"] for a in data["apps"]] == [SLUG]
    assert "streamlit_app.py" in data["apps"][0]["files"]
    assert (out / SLUG / "streamlit_app.py").is_file()


def test_cli_md_output_names_the_excluded_files(tmp_path):
    repo = _repo(tmp_path)
    res = _invoke(SLUG, "--out", str(tmp_path / "bundle"), "--dir", str(repo))
    assert res.exit_code == 0, res.output
    assert SLUG in res.output
    assert "AGENTS.md" in res.output


@pytest.mark.parametrize("case", ["non-empty", "inside-apps", "bad-slug"])
def test_cli_exits_2_on_a_refusal(tmp_path, case):
    repo = _repo(tmp_path)
    out = tmp_path / "bundle"
    slugs: list[str] = []
    if case == "non-empty":
        _write(out / "stale.txt", "x")
    elif case == "inside-apps":
        out = repo / "apps" / "bundle"
    else:
        slugs = ["Not_A_Slug"]
    res = _invoke(*slugs, "--out", str(out), "--dir", str(repo))
    assert res.exit_code == 2, res.output


def test_cli_relative_dir_and_out_resolve_from_cwd(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    res = _invoke("--out", os.path.join("..", "bundle"))
    assert res.exit_code == 0, res.output
    assert (tmp_path / "bundle" / SLUG / "streamlit_app.py").is_file()
