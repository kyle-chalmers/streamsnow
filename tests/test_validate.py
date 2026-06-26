"""Tests for the governance checks + the validate-app aggregate gate."""

from __future__ import annotations

from pathlib import Path

import yaml

from streamsnow.config import Config
from streamsnow.policy import SchemaPolicy
from streamsnow.scaffolder import scaffold
from streamsnow.tools import check_app_security, check_bind_predicates, check_caching
from streamsnow.tools.check_schema_refs import find_denied_refs
from streamsnow.tools.validate_app import validate_app

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"


def _cfg() -> Config:
    return Config.from_dict(yaml.safe_load(EXAMPLE.read_text()))


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_security_flags_egress_exec_and_dynamic_sql(tmp_path):
    p = _write(
        tmp_path / "a.py", "import requests\nimport os\nos.system('x')\nq = f'SELECT * FROM {t}'\n"
    )
    res = check_app_security.scan_paths([p])
    kinds = {f["kind"] for f in res["findings"]}
    assert not res["ok"]
    assert {"egress", "code-exec", "dynamic-sql"} <= kinds


def test_security_flags_write_sql(tmp_path):
    p = _write(tmp_path / "w.sql", "DELETE FROM analytics.t WHERE x=1\n")
    assert not check_app_security.scan_paths([p])["ok"]


def test_security_clean_on_readonly(tmp_path):
    p = _write(tmp_path / "ok.py", "import streamlit as st\nimport plotly.express as px\n")
    assert check_app_security.scan_paths([p])["ok"]


def test_bind_predicate_trap_flagged(tmp_path):
    p = _write(tmp_path / "q.sql", "SELECT 1 WHERE (:1 IS NULL OR col = :1)\n")
    assert not check_bind_predicates.scan_paths([p])["ok"]


def test_caching_flags_uncached_fetch_and_respects_noqa(tmp_path):
    bad = _write(
        tmp_path / "bad.py",
        "import streamlit as st\ndef load():\n    return st.connection('snowflake').query('x')\n",
    )
    assert not check_caching.scan_paths([bad])["ok"]
    ok = _write(
        tmp_path / "ok.py",
        "import streamlit as st\n@st.cache_data(ttl=1800)\ndef load():\n    return st.connection('snowflake').query('x')\n",
    )
    assert check_caching.scan_paths([ok])["ok"]
    noqa = _write(
        tmp_path / "n.py",
        "import streamlit as st\ndef load():  # noqa: cache-required\n    return st.connection('snowflake').query('x')\n",
    )
    assert check_caching.scan_paths([noqa])["ok"]


def test_validate_app_passes_on_scaffold(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "good-app")
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/good-app", policy, cfg.runtime)
    assert res["ok"], res["checks"]


def test_security_flags_python_write_sql_and_format_sql(tmp_path):
    w = _write(
        tmp_path / "w.py",
        "import streamlit as st\ndef f():\n    return st.connection('x').query('DELETE FROM analytics.t')\n",
    )
    assert any(x["kind"] == "write-sql" for x in check_app_security.scan_paths([w])["findings"])
    d = _write(
        tmp_path / "d.py", "def f(c, t):\n    return c.query('SELECT * FROM {}'.format(t))\n"
    )
    assert any(x["kind"] == "dynamic-sql" for x in check_app_security.scan_paths([d])["findings"])


def test_schema_refs_use_statement_and_read_exceptions():
    from streamsnow.policy import SchemaPolicy as SP

    policy = SP(database="DB", schema_allow=("ANALYTICS",), schema_deny=("RAW",))
    assert find_denied_refs("USE SCHEMA RAW;", policy)
    assert find_denied_refs("use schema raw", policy)
    exc = SP(
        database="DB",
        schema_allow=("ANALYTICS",),
        schema_deny=("RAW",),
        read_exceptions=("DB.RAW.SANCTIONED",),
    )
    assert not find_denied_refs("SELECT * FROM DB.RAW.SANCTIONED", exc)
    assert find_denied_refs("SELECT * FROM DB.RAW.OTHER", exc)


def test_validate_app_fails_on_invalid_manifest(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "m-app")
    (tmp_path / "apps/m-app/snowflake.yml").write_text("entities: [oops\n")  # invalid YAML
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/m-app", policy, cfg.runtime)
    by_name = {c["name"]: c["ok"] for c in res["checks"]}
    assert by_name["manifest"] is False
    assert res["ok"] is False


def test_validate_app_fails_on_violations(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "bad-app")
    _write(tmp_path / "apps/bad-app/pages/leak.py", "import requests\n")
    _write(tmp_path / "apps/bad-app/queries/bad.sql", "SELECT * FROM RAW.secrets\n")
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/bad-app", policy, cfg.runtime)
    by_name = {c["name"]: c["ok"] for c in res["checks"]}
    assert res["ok"] is False
    assert by_name["app-security"] is False
    assert by_name["schema-refs"] is False
