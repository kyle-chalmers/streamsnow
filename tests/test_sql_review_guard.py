"""The read-only guard under sql_review: masking, statement splitting, the
statement-root allowlist, and the write-verb tripwire.

These pin behaviour that survived several real bypasses (single, double and
dollar quoting, backslash escapes, `//` comments, verbs smuggled after a CTE
close or into a SET expression). The guard is unchanged by the 0.8 page-file
format, so these tests are carried over from the 0.7 suite as they were; the
page-file behaviour is tested in test_sql_review.py.
"""

from __future__ import annotations

import pytest

from streamsnow.tools import sql_review as sr


def test_set_session_variable_allowed_but_arbitrary_set_not() -> None:
    assert sr._verify_read_only("SET start_date = CURRENT_DATE;\nSELECT 1;") == []
    assert sr._verify_read_only("GRANT ROLE admin TO USER x;") != []


def test_with_cte_prefixed_write_refused() -> None:
    # A CTE prefix proves nothing about the terminal statement.
    assert sr._verify_read_only("WITH x AS (SELECT 1) DELETE FROM t;") != []
    assert sr._verify_read_only("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x;") != []
    # Legitimate shapes stay allowed: single, multi, column-list, nested parens.
    assert sr._verify_read_only("WITH x AS (SELECT 1) SELECT * FROM x;") == []
    assert (
        sr._verify_read_only(
            "WITH a (c) AS (SELECT 1), b AS (SELECT MAX(c) FROM (SELECT c FROM a)) SELECT * FROM b;"
        )
        == []
    )


def test_string_literal_paren_cannot_smuggle_a_cte_write() -> None:
    """The confirmed bypass: a literal containing ')SELECT' collapsed a raw
    paren counter and the DELETE read as a SELECT. Masked scanning closes it."""
    evil = "WITH x AS (SELECT ')SELECT' AS s FROM t) DELETE FROM x;"
    assert sr._verify_read_only(evil) != []


def test_semicolon_in_block_comment_is_legit() -> None:
    assert sr._verify_read_only("SELECT 1 /* note; DROP TABLE x */ FROM t;") == []


def test_escaped_quotes_handled() -> None:
    assert sr._verify_read_only("SELECT 'it''s; fine' FROM t;") == []


def test_unterminated_literal_fails_closed() -> None:
    # Masking consumes to end; a WITH that can't be parsed is not allowed.
    assert sr._verify_read_only("WITH x AS (SELECT 'unterminated) DELETE FROM t;") != []


@pytest.mark.parametrize(
    "sql",
    [
        # Double-quoted DELIMITED IDENTIFIER hiding a `)` — Snowflake treats
        # "..." as an identifier, not a string, so it was initially unmasked:
        # the `)` closed the CTE scan early and the trailing SELECT read as the
        # terminal verb while Snowflake executed the DELETE.
        'WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM target;',
        # Single-quoted literal, the original bypass.
        "WITH x AS (SELECT ')SELECT' AS s FROM t) DELETE FROM x;",
        # Block comment hiding the same trick.
        "WITH x AS (SELECT 1 /* ) SELECT */ ) DELETE FROM t;",
    ],
)
def test_read_only_guard_rejects_quote_hidden_writes(sql: str) -> None:
    assert sr._verify_read_only(sql), f"guard accepted a write statement: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "weird col name" FROM ANALYTICS.ORDERS;',
        'SELECT 1 AS "quoted "" escaped" FROM ANALYTICS.ORDERS;',
        "WITH x AS (SELECT 1) SELECT * FROM x;",
        "SELECT 'a string with ) parens' FROM t;",
    ],
)
def test_read_only_guard_allows_legitimate_quoting(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"guard rejected valid read-only SQL: {sql}"


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("WITH x AS (SELECT $$ ) SELECT y $$) DELETE FROM t;", "dollar-quoted constant"),
        ("WITH x AS (SELECT '\\') SELECT y') DELETE FROM t;", "backslash-escaped quote"),
        ('WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM t;', "delimited identifier"),
        ("WITH x AS (SELECT ')SELECT' AS s FROM t) DELETE FROM x;", "string literal"),
    ],
)
def test_every_quoting_form_is_masked_for_structure(sql: str, why: str) -> None:
    assert sr._verify_read_only(sql), f"bypass via {why}"


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("WITH x AS (SELECT $$ ) SELECT y $$) DELETE FROM t;", "dollar-quoted constant"),
        ("WITH x AS (SELECT '\\') SELECT y') DELETE FROM t;", "backslash-escaped quote"),
        ('WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM t;', "delimited identifier"),
        ("WITH x AS (SELECT ')SELECT' AS s FROM t) DELETE FROM x;", "string literal"),
    ],
)
def test_masking_itself_defeats_each_bypass(sql: str, why: str) -> None:
    """Exercise the MASKER, not just the aggregate verdict.

    `_verify_read_only` now also carries a write-verb tripwire, which would
    reject all of these even if masking regressed — so asserting only on the
    verdict would let a masking bug pass unnoticed. These assert on the thing
    the bypasses actually attacked: the terminal verb the CTE walker reads out
    of masked text.
    """
    masked = sr._mask_strings_and_comments(sql).strip().rstrip(";").strip()
    assert sr._with_terminal_verb(masked) != "SELECT", (
        f"masking still lets {why} pose as a terminal SELECT"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $$hello$$ AS greeting FROM ANALYTICS.ORDERS;",
        "SELECT 'it\\'s fine' FROM ANALYTICS.ORDERS;",
        # A delimited CTE name is legal Snowflake; refusing it would block
        # generating a legitimate audit file, which is also a defect.
        'WITH "cte name" AS (SELECT 1) SELECT * FROM "cte name";',
        'WITH RECURSIVE "r" AS (SELECT 1) SELECT * FROM "r";',
    ],
)
def test_masking_does_not_reject_legitimate_sql(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


def test_dollar_quote_masking_does_not_regress_the_double_quote_fix() -> None:
    """An odd `"` inside `$$…$$` must not blind the guard to later statements.

    Making `"` a delimiter without teaching the masker about `$$` turned a
    write that 0.6.1 CAUGHT into one that passed: the unbalanced quote masked
    to end-of-text and hid every following statement.
    """
    sql = 'SELECT $$5" pipe$$ AS a; DELETE FROM t;'
    assert sr._verify_read_only(sql), "dollar-quote/double-quote interaction regressed"
    # It must also not hide a surviving bind on a later line.
    sql2 = 'SELECT $$5" pipe$$ AS a;\nSELECT b FROM t WHERE d <= :3;\n'
    assert sr._verify_binds_bound(sql2), "unbalanced quote hid a live bind"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $$refreshes at 12:30 UTC$$ AS note FROM t;",
        "SELECT $$see http://host:8080/x$$ AS url FROM t;",
        # Snowflake semi-structured access with a numeric key is not a bind.
        "SELECT payload:1 FROM t;",
        "SELECT b:2 FROM t;",
        "SELECT 1::INT FROM t;",
    ],
)
def test_bind_check_does_not_falsely_refuse_valid_sql(sql: str) -> None:
    """A false positive here refuses to generate a legitimate audit file, and
    the remedy the message prescribes cannot fix it."""
    assert sr._verify_binds_bound(sql) == [], f"false positive on: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1) DELETE FROM t;",
        'SELECT $$5" pipe$$ AS a; DELETE FROM t;',
        "COMMENT ON TABLE t IS 'x';",
        "UNDROP TABLE t;",
        "TRUNCATE TABLE t;",
        "GRANT SELECT ON t TO ROLE r;",
    ],
)
def test_tripwire_catches_writes_in_statement_start_position(sql: str) -> None:
    assert sr._verify_read_only(sql), f"write slipped through: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        # These verbs are not reserved words in Snowflake, so they are legal
        # bare column aliases. Rejecting them refuses a valid audit file.
        "SELECT 1 AS CALL FROM t;",
        "SELECT 1 AS COPY FROM t;",
        "SELECT 1 AS PUT FROM t;",
        "SELECT 1 AS REMOVE FROM t;",
        "SELECT 1 AS UNLOAD FROM t;",
        "SELECT 1 AS EXECUTE FROM t;",
        # Suffix/prefix names never match, thanks to the word boundary.
        "SELECT CALLBACK_TS, COMPUTED_PUT_RATIO, COPY_COUNT FROM t;",
        "SELECT LAST_VALUE(x) OVER (ORDER BY d) FROM t;",
        # Quoted identifiers are masked before the tripwire sees them.
        'SELECT "CREATE", "ALTER", "CALL" FROM t;',
        # The one legal write-shaped root, already validated by the allowlist.
        "SET start_date = CURRENT_DATE;",
    ],
)
def test_tripwire_does_not_refuse_legitimate_read_only_sql(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


def test_tripwire_fires_independently_of_the_allowlist() -> None:
    """Bind to the layer itself, so removing it fails a test.

    Each bypass is fed to the tripwire regexes directly. The allowlist cannot
    mask the result because it is not consulted here.
    """
    for sql in (
        "WITH x AS (SELECT 1) DELETE FROM t",
        'WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM t',
        "WITH x AS (SELECT $$ ) SELECT y $$) DELETE FROM t",
    ):
        masked = sr._mask_strings_and_comments(sql)
        assert sr._WRITE_VERB_AFTER_PAREN_RE.search(masked), f"tripwire blind to: {sql}"
    assert sr._WRITE_VERB_AT_START_RE.search("DELETE FROM t")
    assert sr._WRITE_VERB_AT_START_RE.search("COMMENT ON TABLE t IS 'x'")


def test_set_rooted_statement_does_not_escape_both_layers() -> None:
    """`SET x = (SELECT 1) DELETE FROM t` passed BOTH layers.

    The tripwire used `search` (first match only) and then skipped the whole
    statement on the SET exemption, while the allowlist's SET form is a
    prefix-only regex — so everything after the `=` was examined by neither.
    """
    assert sr._verify_read_only("SET x = (SELECT 1) DELETE FROM t;")


@pytest.mark.parametrize(
    "sql",
    [
        # Bare (un-AS'd, unquoted) column aliases after a `)`. Legal Snowflake:
        # these verbs are not reserved words. `comment` is a real
        # INFORMATION_SCHEMA.TABLES column that discovery queries select.
        "SELECT MAX(d) comment FROM ANALYTICS.ORDERS;",
        "SELECT LISTAGG(x, chr(44)) copy FROM ANALYTICS.ORDERS;",
        "SELECT IFF(a,b,c) merge FROM ANALYTICS.ORDERS;",
        "SELECT COUNT(*) call FROM ANALYTICS.ORDERS;",
    ],
)
def test_bare_alias_after_paren_is_not_a_write(sql: str) -> None:
    """Refusing to generate a legitimate audit file is its own defect."""
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1) MERGE INTO t USING s ON 1=1",
        "WITH x AS (SELECT 1) TRUNCATE TABLE t",
        "WITH x AS (SELECT 1) COMMENT ON TABLE t IS 1",
        "WITH x AS (SELECT 1) COPY INTO t FROM @s",
        "WITH x AS (SELECT 1) DELETE FROM t",
    ],
)
def test_after_paren_anchor_sees_non_reserved_commands_too(sql: str) -> None:
    """The tripwire must not be blind to the non-reserved write commands.

    Restricting the after-paren anchor to RESERVED words (to stop it refusing
    bare column aliases) left it blind to `) MERGE INTO t` and
    `) TRUNCATE TABLE t`. The allowlist catches those today, but this layer
    exists to hold when the walker is fooled, so omitting them traded away the
    exact coverage it is for. Matched in two-token command form instead.
    """
    masked = sr._mask_strings_and_comments(sql)
    assert sr._WRITE_VERB_AFTER_PAREN_RE.search(masked), f"tripwire blind to: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        # The same verbs as BARE aliases: followed by FROM, never INTO/TABLE/ON.
        "SELECT COUNT(*) merge FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) truncate FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) put FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) remove FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) comment FROM ANALYTICS.ORDERS;",
        "SELECT LISTAGG(x, chr(44)) copy FROM ANALYTICS.ORDERS;",
    ],
)
def test_two_token_commands_do_not_fire_on_bare_aliases(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        # A subquery aliased with a write-verb name, followed by a JOIN's ON.
        # `) comment ON a.id = ...` looks exactly like `COMMENT ON`, and this
        # was a real false positive: legal read-only SQL refused.
        "SELECT a.x FROM t a JOIN (SELECT 1 AS id) comment ON a.id = comment.id;",
        "SELECT a.x FROM t a JOIN (SELECT 1 AS id) copy ON a.id = copy.id;",
        "SELECT a.x FROM t a LEFT JOIN (SELECT 1 AS id) merge ON a.id = merge.id;",
        "SELECT * FROM (SELECT 1) remove;",
    ],
)
def test_join_alias_named_after_a_write_verb_is_allowed(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        "COMMENT ON TABLE t IS 'x';",
        "WITH x AS (SELECT 1) COMMENT ON TABLE t IS 1;",
        "WITH x AS (SELECT 1) COMMENT ON VIEW v IS 1;",
        "WITH x AS (SELECT 1) COMMENT ON COLUMN t.c IS 1;",
    ],
)
def test_real_comment_ddl_is_still_refused(sql: str) -> None:
    """Narrowing `COMMENT ON` must not lose the DDL it exists to catch."""
    assert sr._verify_read_only(sql), f"COMMENT DDL slipped through: {sql}"


@pytest.mark.parametrize(
    "expr",
    [
        "(SELECT MAX(load_date) FROM REPORTING.VW_ORDERS)::DATE",
        "(SELECT MAX(load_date) FROM REPORTING.VW_ORDERS) - 1",
        "(SELECT COUNT(*) FROM ANALYTICS.ORDERS) / 2",
        "(SELECT MAX(v) FROM ANALYTICS.ORDERS) || '-x'",
        "COALESCE((SELECT MAX(d) FROM ANALYTICS.ORDERS), CURRENT_DATE)",
        "(SELECT MAX(d) FROM ANALYTICS.ORDERS)",
        "CURRENT_DATE",
        "DATEADD('day', -30, CURRENT_DATE)",
        # An identifier that merely CONTAINS a verb must not trip the scan.
        "(SELECT MAX(create_date) FROM ANALYTICS.UPDATES)",
        "(SELECT MAX(d) FROM ANALYTICS.ORDERS WHERE action = 'DELETE')",
    ],
)
def test_legal_set_block_expressions_are_accepted(expr: str) -> None:
    assert sr._verify_read_only(f"SET end_date = {expr};") == [], f"refused: {expr}"


@pytest.mark.parametrize(
    "expr",
    [
        "(SELECT 1) DELETE FROM t",
        "(SELECT 1) CALL MY_PROC()",
        "(SELECT 1) UNLOAD TO @s",
        "(SELECT 1) UNSET y",
        "1 DROP TABLE t",
        # Wrapping it in outer parens hid the command inside the group.
        "((SELECT 1) CALL MY_PROC())",
        "((SELECT 1) DELETE FROM t)",
    ],
)
def test_command_smuggled_into_a_set_expression_is_refused(expr: str) -> None:
    assert sr._verify_read_only(f"SET x = {expr};"), f"smuggled command passed: {expr}"


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("WITH x AS (SELECT 1) TRUNCATE ANALYTICS.ORDERS", "TABLE is optional in Snowflake"),
        ("WITH x AS (SELECT 1) TRUNCATE TABLE t", "explicit TABLE"),
        ("WITH x AS (SELECT 1) UNDROP SCHEMA ANALYTICS", "UNDROP takes SCHEMA too"),
        ("WITH x AS (SELECT 1) EXECUTE TASK MY_TASK", "EXECUTE takes TASK too"),
        ("WITH x AS (SELECT 1) RM @MY_STAGE/f.csv", "RM is REMOVE's alias"),
        ("WITH x AS (SELECT 1) CALL SYSTEM$ABORT_SESSION(1)", "CALL"),
        ("WITH x AS (SELECT 1) UNLOAD TO @s", "UNLOAD"),
        ("WITH x AS (SELECT 1) UNSET my_var", "UNSET"),
    ],
)
def test_after_paren_anchor_covers_every_write_command_form(sql: str, why: str) -> None:
    masked = sr._mask_strings_and_comments(sql)
    assert sr._WRITE_VERB_AFTER_PAREN_RE.search(masked), f"tripwire blind to {why}: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        # Bare aliases named after write verbs, followed by a CLAUSE keyword.
        "SELECT COUNT(*) call FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) truncate FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) unset FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) execute FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) undrop FROM ANALYTICS.ORDERS;",
        "SELECT COUNT(*) call, 1 AS y FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) truncate WHERE 1=1;",
        # A widened pattern must not match a longer identifier.
        "SELECT f(x) copy INTO_TAB FROM ANALYTICS.ORDERS;",
    ],
)
def test_widened_patterns_do_not_refuse_bare_aliases(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


@pytest.mark.parametrize(
    ("sql", "form"),
    [
        ("SELECT ' ;\nDELETE FROM prod.t;", "string literal"),
        ('SELECT " ;\nDELETE FROM prod.t;', "quoted identifier"),
        ("SELECT $$ ;\nDELETE FROM prod.t;", "dollar-quoted constant"),
        ("SELECT 1 /* ;\nDELETE FROM prod.t;", "block comment"),
    ],
)
def test_unterminated_quoting_fails_closed(sql: str, form: str) -> None:
    """An open quoting form masks to end-of-text, blinding EVERY guard after it.

    Reachable by ordinary error, not attack: a token literal containing an
    apostrophe (`AND last_name = 'O'Brien'`) is enough. Both generate and check
    reported success on a file whose header claimed no section referenced a
    session variable while a section referenced two undeclared ones.
    """
    problems = sr._verify_read_only(sql)
    assert problems, f"blind after an unterminated {form}"
    assert "unterminated" in problems[0]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'ok' FROM ANALYTICS.ORDERS;",
        'SELECT "col name" FROM ANALYTICS.ORDERS;',
        "SELECT $$body$$ FROM ANALYTICS.ORDERS;",
        "SELECT 1 /* note */ FROM ANALYTICS.ORDERS;",
        "SELECT 'it''s' FROM ANALYTICS.ORDERS;",
        "SELECT 'it\\'s' FROM ANALYTICS.ORDERS;",
    ],
)
def test_closed_quoting_is_not_flagged_as_unterminated(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


def test_tripwire_wiring_holds_when_the_walker_is_fooled(monkeypatch) -> None:
    """Bind to the CALL PATH, not the regex objects.

    The previous test asserted on `_WRITE_VERB_*_RE.search(...)` directly, so
    disabling the code that CONSULTS them left the suite green — the whole
    defence-in-depth layer could be deleted by a refactor. Simulating a fooled
    walker is the contract: the allowlist is satisfied, and the tripwire must
    still refuse.
    """
    monkeypatch.setattr(sr, "_with_terminal_verb", lambda stmt: "SELECT")
    assert sr._verify_read_only("WITH x AS (SELECT 1) DELETE FROM t;"), (
        "after-paren anchor is not wired into _verify_read_only"
    )
    # The START anchor is inherently redundant with the root allowlist: any
    # statement beginning with a write verb is already refused by its root. To
    # bind its WIRING, simulate a compromised allowlist by admitting the root,
    # so only the tripwire can refuse it.
    monkeypatch.setattr(sr, "ALLOWED_ROOTS", sr.ALLOWED_ROOTS | {"DELETE"})
    assert sr._verify_read_only("DELETE FROM t;"), "start anchor is not wired in"


@pytest.mark.parametrize(
    "alias",
    ["comment", "copy", "merge", "call", "truncate", "unset", "execute", "undrop", "put"],
)
def test_legal_alias_inside_a_set_expression_is_accepted(alias: str) -> None:
    """The same fragment is accepted after `)`; refusing it here contradicted
    the tool's own pinned behaviour and made the coverage gate unsatisfiable."""
    sql = f"SET end_date = (SELECT MAX(d) {alias} FROM ANALYTICS.ORDERS)::DATE;"
    assert sr._verify_read_only(sql) == [], f"refused legal alias {alias!r}"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT METADATA$FILENAME FROM @RAW_FILES/inbound/",
        "SELECT METADATA$FILE_ROW_NUMBER FROM @RAW_FILES/inbound/",
        "SELECT SYSTEM$TYPEOF(x) FROM ANALYTICS.ORDERS",
        "SELECT SYSTEM$CLUSTERING_INFORMATION('ANALYTICS.ORDERS')",
        "SELECT $1 FROM @RAW_FILES/inbound/",
    ],
)
def test_dollar_inside_an_identifier_is_not_a_session_variable(sql: str) -> None:
    assert sr._verify_session_vars_defined(sql) == [], f"false positive on: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT MAX(d) call FETCH FIRST 1 ROWS ONLY;",
        "SELECT MAX(d) call MINUS SELECT 1;",
        "SELECT ROUND(d) truncate FETCH FIRST 1 ROWS ONLY;",
        "SELECT MAX(d) unset FETCH FIRST 1 ROWS ONLY;",
        "SELECT * FROM (SELECT a FROM t) call SAMPLE (10);",
        "SELECT * FROM (SELECT a FROM t) call TABLESAMPLE (10);",
    ],
)
def test_clause_keyword_list_covers_the_less_common_clauses(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


def test_multi_variable_set_form_is_recognised_both_halves() -> None:
    """Both halves of the `SET (a, b) = (...)` fix were uncovered."""
    assert sr._SET_STMT_RE.match("SET (start_date, end_date) = (1, 2)"), "regex half"
    body = "SET (start_date, end_date) = (1, 2);\nSELECT $start_date, $end_date FROM t;"
    assert sr._verify_session_vars_defined(body) == [], "definition half"


@pytest.mark.parametrize(
    "sql",
    [
        # `$` is legal inside an unquoted Snowflake identifier, so `x$$y` is a
        # column name, not an unterminated dollar-quote. The fail-closed guard
        # refused this whole file.
        "SELECT x$$y FROM ANALYTICS.ORDERS;",
        "SELECT a$$ FROM ANALYTICS.ORDERS;",
        "SELECT t.col$$1 FROM ANALYTICS.ORDERS t;",
        # A real dollar-quote after punctuation/space must still work.
        "SELECT ($$body$$) FROM ANALYTICS.ORDERS;",
        "SELECT $$abc$$ FROM ANALYTICS.ORDERS;",
    ],
)
def test_dollar_inside_identifier_is_not_a_dollar_quote(sql: str) -> None:
    _, unterminated = sr._mask_with_status(sql)
    assert unterminated is None, f"falsely unterminated: {sql}"
    assert sr._verify_read_only(sql) == [], f"refused legal SQL: {sql}"


def test_real_unterminated_dollar_quote_still_fails_closed() -> None:
    assert sr._verify_read_only("SELECT $$ ;\nDELETE FROM t;")


@pytest.mark.parametrize(
    "tail",
    [
        "COMMENT IF EXISTS ON TABLE t IS 'x'",
        "COMMENT ON TAG t1 IS 'x'",
        "COMMENT ON MASKING POLICY p IS 'x'",
        "COMMENT ON DYNAMIC TABLE dt IS 'x'",
        "COMMENT ON SHARE s IS 'x'",
    ],
)
def test_comment_command_forms_are_refused_in_a_set_expression(tail: str) -> None:
    assert not sr._valid_set_statement(f"SET x = (SELECT 1) {tail}"), f"slipped: {tail}"
    assert sr._verify_read_only(f"SET x = (SELECT 1) {tail};")


def test_join_alias_comment_before_on_is_still_legal() -> None:
    sql = "SELECT a.x FROM t a JOIN (SELECT 1 AS id) comment ON a.id = comment.id;"
    assert sr._verify_read_only(sql) == []


def test_double_slash_line_comment_is_masked() -> None:
    """`//` is a documented Snowflake line comment. Sixth bypass of the class.

    An apostrophe inside one opened a phantom string literal that ran to the
    next `'` in the file and hid real SQL from every guard — and because the
    phantom literal TERMINATED, the fail-closed path never fired.
    """
    sql = "SELECT 1 // it's\n; DELETE FROM t WHERE x = :1 AND y = $undeclared; -- that's\n"
    assert sr._verify_read_only(sql), "DELETE hidden behind a // comment"
    assert sr._verify_binds_bound(sql), ":1 hidden behind a // comment"
    assert sr._verify_session_vars_defined(sql), "$undeclared hidden behind a // comment"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 // it's fine\nFROM ANALYTICS.ORDERS;",
        "SELECT 'http://x.example/y' FROM ANALYTICS.ORDERS;",  # // inside a literal
        "SELECT $$a // b$$ FROM ANALYTICS.ORDERS;",  # // inside a $$ body
        "SELECT a / b FROM ANALYTICS.ORDERS;",  # a lone slash is division
    ],
)
def test_double_slash_handling_does_not_refuse_legal_sql(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


@pytest.mark.parametrize(
    "tail",
    [
        "CALL start()",
        "CALL changes()",
        "CALL sample()",
        "CALL db.sch.p()",
        "CALL SYSTEM$WAIT(1)",
        "COPY FILES INTO @s2 FROM @s1",
        "UNDROP ICEBERG TABLE t",
        "UNDROP DYNAMIC TABLE t",
        "TRUNCATE IF EXISTS t",
    ],
)
def test_more_command_forms_are_refused_in_a_set_expression(tail: str) -> None:
    assert not sr._valid_set_statement(f"SET x = (SELECT 1) {tail}"), f"slipped: {tail}"


def test_procedure_named_after_a_clause_keyword_is_still_a_call() -> None:
    """Round 8's clause exclusion had turned `CALL start()` into a non-command."""
    masked = sr._mask_strings_and_comments(
        "WITH c AS (SELECT 1) SELECT * FROM (SELECT 1) CALL start()"
    )
    assert sr._WRITE_VERB_AFTER_PAREN_RE.search(masked)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT 1 a) call NATURAL JOIN u;",
        "SELECT * FROM (SELECT 1 a) call ASOF JOIN u MATCH_CONDITION(a.t >= u.t);",
        "SELECT COUNT(*) call FROM (SELECT 1) x;",  # bare alias, space before paren
    ],
)
def test_bare_alias_before_join_forms_is_legal(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


def test_tripwire_message_names_only_the_verb() -> None:
    """The reported verb must be `TRUNCATE`, never `TRUNCATE N` (first char of
    the identifier captured by the pattern). The allowlist reports first for a
    WITH statement, so look at the tripwire's own line, not problems[0]."""
    problems = sr._verify_read_only("WITH x AS (SELECT 1) TRUNCATE t;")
    tripwire = [p for p in problems if "command position" in p]
    assert tripwire, problems
    assert "'TRUNCATE'" in tripwire[0] and "'TRUNCATE N" not in tripwire[0], tripwire


def test_bind_regex_lookbehind_is_pinned() -> None:
    """The `'`, `)`, `]` exclusions changed bind detection with no coverage."""
    for sql in ("d <= :1", "(:1", ",:1", "= :1", "x=:1"):
        assert sr._BIND_RE.search(sql), f"real bind missed: {sql}"
    for sql in ("f(a):1", "arr[0]:1", "payload:1", "a::1"):
        assert not sr._BIND_RE.search(sql), f"non-bind matched: {sql}"


def test_semicolon_inside_string_literal_is_legit() -> None:
    """Over-rejection fix: a ; or verb inside a literal must not split the
    statement into a fragment with a disallowed root."""
    ok = "SELECT 'a; DROP TABLE x' AS s FROM ANALYTICS_DB.REPORTING.VW_REVENUE_DAILY;"
    assert sr._verify_read_only(ok) == []
