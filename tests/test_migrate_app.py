"""Tests for the /migrate-app detection engine (streamsnow.tools.migrate_app).

Fixtures model a fictional Acme retail-analytics team migrating an external
legacy dashboard (pages/-directory layout, environment.yml conda pins, inline
SQL) into a StreamSnow repo. All translate-deps tests run offline — no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from streamsnow.config import Config
from streamsnow.policy import SchemaPolicy
from streamsnow.tools.migrate_app import (
    graft_plan,
    main,
    preflight,
    scan_conformance,
    scan_hardfails,
    scan_imports,
    scan_inline_sql,
    translate_deps,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"


def _cfg() -> Config:
    return Config.from_dict(yaml.safe_load(EXAMPLE.read_text()))


def _policy() -> SchemaPolicy:
    return SchemaPolicy.from_governance(_cfg().governance)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


ENTRYPOINT = (
    "import streamlit as st\n\n"
    'st.set_page_config(page_title="Acme Sales", layout="wide")\n'
    'st.title("Acme Sales")\n'
)

# Legacy page: uncached fetch, SELECT *, altair import, one inline SQL literal.
LEGACY_PAGE = (
    "import streamlit as st\n"
    "import altair as alt\n\n\n"
    "def load_orders():\n"
    '    conn = st.connection("snowflake")\n'
    '    return conn.query("SELECT * FROM ANALYTICS_DB.ANALYTICS.ORDERS")\n'
)


def _acme_source(root: Path) -> Path:
    """External legacy Acme app: pages/ layout + environment.yml conda pins."""
    src = root / "acme-src"
    _write(src / "app.py", ENTRYPOINT)
    _write(src / "pages" / "10_revenue.py", LEGACY_PAGE)
    _write(
        src / "environment.yml",
        "name: acme\nchannels:\n  - defaults\ndependencies:\n"
        "  - streamlit=1.50.0\n  - pandas\n  - python=3.11\n",
    )
    return src


def _repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "apps").mkdir(parents=True)
    return repo


# --------------------------------------------------------------------------- #
# preflight                                                                   #
# --------------------------------------------------------------------------- #


def test_preflight_passes_on_acme_source(tmp_path):
    src = _acme_source(tmp_path)
    code, res = preflight(src, "acme-sales-dashboard", _repo(tmp_path))
    assert code == 0
    assert res["is_streamlit_app"] is True
    assert res["entrypoints"] == ["app.py"]
    assert res["deps_manifest"] == "environment.yml"
    assert res["abort"] is False


def test_preflight_aborts_on_target_collision(tmp_path):
    src = _acme_source(tmp_path)
    repo = _repo(tmp_path)
    (repo / "apps" / "acme-sales-dashboard").mkdir()
    code, res = preflight(src, "acme-sales-dashboard", repo)
    assert code == 1
    assert res["abort"] and "already exists" in res["abort_reason"]


def test_preflight_aborts_on_non_streamlit_source(tmp_path):
    src = tmp_path / "plain"
    _write(src / "main.py", "print('hello')\n")
    code, res = preflight(src, "acme-sales-dashboard", _repo(tmp_path))
    assert code == 1
    assert res["is_streamlit_app"] is False


def test_preflight_aborts_on_multiple_entrypoints(tmp_path):
    src = _acme_source(tmp_path)
    _write(src / "second.py", ENTRYPOINT)
    code, res = preflight(src, "acme-sales-dashboard", _repo(tmp_path))
    assert code == 1
    assert len(res["entrypoints"]) == 2
    assert "multiple entrypoints" in res["abort_reason"]


def test_preflight_aborts_on_catastrophic_dep(tmp_path):
    src = tmp_path / "mlapp"
    _write(src / "app.py", ENTRYPOINT)
    # requirements.txt outranks environment.yml in manifest discovery order.
    _write(src / "requirements.txt", "streamlit\ntorch==2.1\n")
    code, res = preflight(src, "acme-ml-dashboard", _repo(tmp_path))
    assert code == 1
    assert res["catastrophic_deps"] == ["torch"]


# --------------------------------------------------------------------------- #
# scan-hardfails                                                              #
# --------------------------------------------------------------------------- #


def test_hardfails_clean_on_acme_source(tmp_path):
    src = _acme_source(tmp_path)
    code, res = scan_hardfails(src, _policy())
    assert code == 0
    assert res["blocks"] is False


def test_hardfails_flags_denied_schema_in_query_call(tmp_path):
    # RAW is on the example config's schema_deny list.
    src = _acme_source(tmp_path)
    _write(
        src / "pages" / "20_raw.py",
        "import streamlit as st\n\n\ndef load():\n"
        '    conn = st.connection("snowflake")\n'
        '    return conn.query("SELECT order_id FROM ANALYTICS_DB.RAW.ORDERS")\n',
    )
    code, res = scan_hardfails(src, _policy())
    assert code == 1
    assert res["blocks"] is True
    hits = [(r["file"], r["schema"]) for r in res["schema_refs"]]
    assert ("pages/20_raw.py", "RAW") in hits


def test_hardfails_flags_denied_schema_in_sql_file(tmp_path):
    src = _acme_source(tmp_path)
    _write(src / "queries" / "orders.sql", "SELECT order_id\nFROM ANALYTICS_DB.RAW.ORDERS\n")
    code, res = scan_hardfails(src, _policy())
    assert code == 1
    assert any(
        r["file"] == "queries/orders.sql" and r["schema"] == "RAW" for r in res["schema_refs"]
    )


def test_hardfails_ignores_docstring_mention_of_denied_schema(tmp_path):
    src = _acme_source(tmp_path)
    _write(
        src / "notes.py",
        '"""Never query RAW.ORDERS from app code — SELECT FROM the governed view."""\n',
    )
    code, res = scan_hardfails(src, _policy())
    assert code == 0


def test_hardfails_flags_hardcoded_secret_but_not_placeholder(tmp_path):
    src = _acme_source(tmp_path)
    _write(
        src / "settings.py",
        'password = "s3cr3t-value-42"\napi_key = "<your-api-key>"\n',
    )
    code, res = scan_hardfails(src, _policy())
    assert code == 1
    kinds = {h["kind"] for h in res["secrets_in_py"]}
    assert kinds == {"password"}  # the placeholder api_key must not be flagged


def test_hardfails_flags_aws_key_shape(tmp_path):
    src = _acme_source(tmp_path)
    # AWS's documented example access key id — not a real credential.
    _write(src / "cloud.py", 'BUCKET_KEY = "AKIAIOSFODNN7EXAMPLE"\n')
    code, res = scan_hardfails(src, _policy())
    assert code == 1
    assert res["secrets_in_py"][0]["kind"] == "aws_access_key_id"


def test_hardfails_records_secrets_file_presence_without_blocking(tmp_path):
    src = _acme_source(tmp_path)
    _write(src / ".streamlit" / "secrets.toml", 'account = "should-never-be-read"\n')
    _write(src / ".env", "X=1\n")
    code, res = scan_hardfails(src, _policy())
    assert code == 0  # presence alone is informational, not a block
    assert res["has_secrets_toml"] is True
    assert res["has_env_file"] is True


# --------------------------------------------------------------------------- #
# translate-deps (all offline — no network)                                   #
# --------------------------------------------------------------------------- #


def test_translate_deps_pep440_to_conda(tmp_path):
    src = tmp_path / "src"
    _write(src / "app.py", ENTRYPOINT)
    _write(
        src / "requirements.txt",
        "streamlit==1.50.0\npandas>=2,<3\nnumpy~=1.26\n",
    )
    out = tmp_path / "environment.yml"
    code, res = translate_deps(src, out, offline=True)
    assert code == 0
    outputs = {t["source"]: t["output"] for t in res["translated"]}
    assert outputs["streamlit"] == "streamlit=1.50.0"  # == becomes single =
    assert outputs["pandas"] == "pandas>=2,<3"  # source order preserved
    assert outputs["numpy"] == "numpy>=1.26,<2"  # ~=X.Y expands to a range
    assert outputs["snowflake-snowpark-python"] == "snowflake-snowpark-python"  # injected
    data = yaml.safe_load(out.read_text())
    assert data["channels"] == ["snowflake"]
    assert "pandas>=2,<3" in data["dependencies"]


def test_translate_deps_drops_python_extras_and_markers(tmp_path):
    src = tmp_path / "src"
    _write(src / "app.py", ENTRYPOINT)
    _write(
        src / "requirements.txt",
        'python==3.11\nplotly[express]>=5\nrequests==2.31.0; python_version < "3.12"\n',
    )
    out = tmp_path / "environment.yml"
    code, res = translate_deps(src, out, offline=True)
    assert code == 0
    reasons = " | ".join(d["reason"] for d in res["dropped"])
    assert len(res["dropped"]) == 3
    assert "python pin" in reasons
    assert "extras" in reasons
    assert "environment marker" in reasons
    # None of the dropped specs may leak into the generated file.
    deps = yaml.safe_load(out.read_text())["dependencies"]
    assert not any(d.split("=")[0] == "python" for d in deps)
    assert not any("plotly" in d or "requests" in d for d in deps)


def test_translate_deps_unmapped_offline_reason(tmp_path):
    src = tmp_path / "src"
    _write(src / "app.py", ENTRYPOINT)
    _write(src / "requirements.txt", "some-obscure-widget==0.1\n")
    code, res = translate_deps(src, tmp_path / "environment.yml", offline=True)
    assert code == 0
    assert res["unmapped"][0]["package"] == "some-obscure-widget"
    assert "verify availability manually" in res["unmapped"][0]["reason"]


def test_translate_deps_from_environment_yml_source(tmp_path):
    # The Acme fixture's source manifest is itself conda-style environment.yml:
    # pins pass through, the python pin is still dropped.
    src = _acme_source(tmp_path)
    out = tmp_path / "out" / "environment.yml"
    code, res = translate_deps(src, out, offline=True)
    assert code == 0
    outputs = {t["source"]: t["output"] for t in res["translated"]}
    assert outputs["streamlit"] == "streamlit=1.50.0"
    assert outputs["pandas"] == "pandas"
    assert any("python pin" in d["reason"] for d in res["dropped"])


def test_translate_deps_no_manifest_infers_suggestions_only(tmp_path):
    src = tmp_path / "src"
    _write(src / "app.py", ENTRYPOINT + "import altair as alt\n")
    _write(src / "pages" / "trend.py", "import altair as alt\nimport helpers\n")
    _write(src / "helpers.py", "import os\n")  # first-party — never suggested
    out = tmp_path / "environment.yml"
    code, res = translate_deps(src, out, offline=True)
    assert code == 0
    assert res["translated"] == []
    suggestions = {s["package"]: s for s in res["inferred_suggestions"]}
    assert suggestions["altair"]["confidence"] == "high"  # imported from 2 files
    assert "helpers" not in suggestions and "os" not in suggestions
    # Default env.yml carries only the required deps — suggestions never auto-add.
    deps = yaml.safe_load(out.read_text())["dependencies"]
    assert deps == ["streamlit=1.50.0", "snowflake-snowpark-python"]


# --------------------------------------------------------------------------- #
# graft-plan                                                                  #
# --------------------------------------------------------------------------- #


def test_graft_plan_pages_dir_wins(tmp_path):
    src = _acme_source(tmp_path)
    code, res = graft_plan(src)
    assert code == 0
    assert res["graft_target"] == "pages/*"
    assert res["source_has_pages"] is True


def test_graft_plan_single_file_with_tabs_goes_to_overview(tmp_path):
    src = tmp_path / "tabbed"
    _write(
        src / "app.py",
        ENTRYPOINT + 'tab1, tab2 = st.tabs(["Revenue", "Regions"])\n',
    )
    code, res = graft_plan(src)
    assert res["graft_target"] == "pages/overview.py"
    assert res["source_uses_st_tabs"] is True


def test_graft_plan_single_file_no_tabs_merges_into_entrypoint(tmp_path):
    src = tmp_path / "single"
    _write(src / "app.py", ENTRYPOINT)
    code, res = graft_plan(src)
    assert res["graft_target"] == "streamlit_app.py"
    assert res["source_entrypoint_count"] == 1


def test_graft_plan_ignores_function_scope_tabs(tmp_path):
    src = tmp_path / "fn-tabs"
    _write(
        src / "app.py",
        ENTRYPOINT + 'def render():\n    t1, t2 = st.tabs(["A", "B"])\n',
    )
    code, res = graft_plan(src)
    assert res["source_uses_st_tabs"] is False
    assert res["graft_target"] == "streamlit_app.py"


# --------------------------------------------------------------------------- #
# scan-imports                                                                #
# --------------------------------------------------------------------------- #


def test_scan_imports_lists_relative_imports_and_subpackages(tmp_path):
    src = _acme_source(tmp_path)
    _write(src / "utils" / "__init__.py", "")
    _write(src / "utils" / "fmt.py", "PCT = '{:.1%}'\n")
    _write(src / "pages" / "30_regions.py", "from .helpers import region_label\n")
    code, res = scan_imports(src)
    assert code == 0
    modules = {r["module"] for r in res["relative_imports"]}
    assert ".helpers" in modules
    assert "utils/__init__.py" in res["subpackage_init_files"]


# --------------------------------------------------------------------------- #
# scan-conformance                                                            #
# --------------------------------------------------------------------------- #


def _lifted_app(root: Path) -> Path:
    """The Acme source as it looks right after the lift step (pre-conform)."""
    app = root / "apps" / "acme-sales-dashboard"
    _write(app / "streamlit_app.py", ENTRYPOINT)  # no st.navigation yet
    _write(app / "pages" / "10_revenue.py", LEGACY_PAGE)
    return app


def test_scan_conformance_finds_the_conform_worklist(tmp_path):
    app = _lifted_app(tmp_path)
    code, res = scan_conformance(app, _cfg())
    assert code == 0
    assert res["legacy_pages_only"] is True
    assert [u["func"] for u in res["uncached_queries"]] == ["load_orders"]
    assert res["select_stars"][0]["file"] == "pages/10_revenue.py"
    assert res["altair_imports"][0]["file"] == "pages/10_revenue.py"


def test_scan_conformance_required_grants_split(tmp_path):
    app = _lifted_app(tmp_path)
    _write(
        app / "pages" / "20_events.py",
        "import streamlit as st\n\n\n"
        "@st.cache_data(ttl=1800)\n"
        "def load_events():\n"
        '    conn = st.connection("snowflake")\n'
        "    return conn.query(\n"
        '        "SELECT event_id FROM VENDOR_DB.EXTERNAL.EVENTS"\n'
        "    )\n",
    )
    _, res = scan_conformance(app, _cfg())
    grants = {(g["database"], g["schema"]): g for g in res["required_grants"]}
    # ANALYTICS_DB.ANALYTICS is governance.database × schema_allow → default grant.
    assert grants[("ANALYTICS_DB", "ANALYTICS")]["granted_by_default"] is True
    assert grants[("VENDOR_DB", "EXTERNAL")]["granted_by_default"] is False
    assert "GRANT" in grants[("VENDOR_DB", "EXTERNAL")]["reason"]
    # Default-granted pairs sort first.
    assert res["required_grants"][0]["granted_by_default"] is True


def test_scan_conformance_respects_cache_noqa_and_docstring_select_star(tmp_path):
    app = tmp_path / "apps" / "acme-ops"
    _write(
        app / "streamlit_app.py",
        "import streamlit as st\n\n\n"
        "def heartbeat():\n"
        '    """Docs may say SELECT * FROM anywhere — prose, not SQL."""\n'
        '    conn = st.connection("snowflake")\n'
        '    return conn.query("SELECT CURRENT_TIMESTAMP()")  # noqa: cache-required\n',
    )
    _, res = scan_conformance(app, _cfg())
    assert res["uncached_queries"] == []
    assert res["select_stars"] == []


def test_scan_conformance_clean_on_conforming_app(tmp_path):
    app = tmp_path / "apps" / "acme-clean"
    _write(
        app / "streamlit_app.py",
        "import streamlit as st\n\n"
        'st.set_page_config(page_title="Acme Clean", layout="wide")\n'
        'pg = st.navigation({"Views": [st.Page("pages/overview.py", title="Overview")]})\n'
        "pg.run()\n",
    )
    _write(
        app / "pages" / "overview.py",
        "import streamlit as st\n\nfrom sql_loader import load_sql\n\n\n"
        "@st.cache_data(ttl=1800)\n"
        "def load_revenue(start_date: str, end_date: str):\n"
        '    conn = st.connection("snowflake")\n'
        '    return conn.query(load_sql("revenue_daily"), params=[start_date, end_date], ttl=0)\n',
    )
    code, res = scan_conformance(app, _cfg())
    assert code == 0
    assert res["uncached_queries"] == []
    assert res["select_stars"] == []
    assert res["altair_imports"] == []
    assert res["legacy_pages_only"] is False
    # And the other scanners agree the conformed app is clean.
    assert scan_hardfails(app, _policy())[1]["blocks"] is False
    assert scan_inline_sql(app)[1]["candidates"] == []


# --------------------------------------------------------------------------- #
# scan-inline-sql                                                             #
# --------------------------------------------------------------------------- #


def test_scan_inline_sql_catches_assign_then_pass(tmp_path):
    app = tmp_path / "apps" / "acme-sales-dashboard"
    _write(
        app / "data.py",
        "def fetch(conn):\n"
        '    sql = """SELECT region, SUM(revenue) FROM REPORTING.VW_REVENUE_DAILY GROUP BY 1"""\n'
        "    return conn.query(sql)\n",
    )
    code, res = scan_inline_sql(app)
    assert code == 0
    (hit,) = res["candidates"]
    assert hit["file"] == "data.py"
    assert hit["function"] == "fetch"
    assert hit["sample"].startswith("SELECT region")


def test_scan_inline_sql_noqa_and_named_loader_are_exempt(tmp_path):
    app = tmp_path / "apps" / "acme-sales-dashboard"
    _write(
        app / "data.py",
        "def fetch(conn, load_sql):\n"
        '    ts = conn.query("SELECT CURRENT_TIMESTAMP() FROM DUAL")  # noqa: inline-sql\n'
        '    return conn.query(load_sql("revenue_daily")), ts\n',
    )
    _, res = scan_inline_sql(app)
    assert res["candidates"] == []


def test_scan_inline_sql_skips_docstrings(tmp_path):
    app = tmp_path / "apps" / "acme-sales-dashboard"
    _write(
        app / "data.py",
        'def fetch():\n    """Runs SELECT revenue FROM the governed view."""\n    return None\n',
    )
    _, res = scan_inline_sql(app)
    assert res["candidates"] == []


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def test_cli_preflight_emits_json_and_exit_code(tmp_path, capsys):
    src = _acme_source(tmp_path)
    repo = _repo(tmp_path)
    rc = main(["preflight", str(src), "--target-slug", "acme-sales-dashboard", "--dir", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["abort"] is False

    (repo / "apps" / "acme-sales-dashboard").mkdir()
    rc = main(["preflight", str(src), "--target-slug", "acme-sales-dashboard", "--dir", str(repo)])
    assert rc == 1


def test_cli_scan_hardfails_uses_config_policy(tmp_path, capsys):
    src = _acme_source(tmp_path)
    _write(src / "bad.py", 'def f(c):\n    return c.query("SELECT a FROM RAW.ORDERS")\n')
    rc = main(["scan-hardfails", str(src), "--config", str(EXAMPLE)])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["blocks"] is True


def test_cli_missing_source_dir_is_tool_error(tmp_path, capsys):
    rc = main(["scan-imports", str(tmp_path / "nope")])
    assert rc == 2
    assert "not a directory" in capsys.readouterr().err


def test_cli_missing_config_is_tool_error(tmp_path, capsys):
    src = _acme_source(tmp_path)
    rc = main(["scan-hardfails", str(src), "--config", str(tmp_path / "no-config.yaml")])
    assert rc == 2
    assert "config error" in capsys.readouterr().err
