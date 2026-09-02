"""Deterministic, AST-based validation pipeline for `query_data`.

`query_data` is the only tool that accepts raw SQL text. Trusting a prompt
or the calling agent's stated intent to keep that SQL read-only is not a
security boundary, so every incoming string is parsed into a real AST
(sqlglot, Postgres dialect) and walked structurally before it is allowed
anywhere near a connection:

    length check -> parse -> statement-type check -> banned-construct scan
    -> table/column authorization -> complexity limits -> LIMIT enforcement

A query that fails any stage is rejected with a `QueryError` /
`AuthorizationError`; nothing is "sanitized" or best-effort here — every
stage is a hard allow/deny.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from backend.mcp_servers.db.config.settings import Settings
from backend.mcp_servers.db.database.connection import Database
from backend.mcp_servers.db.errors import AuthorizationError, QueryError, ValidationError
from backend.mcp_servers.db.security.permissions import PermissionsConfig

DIALECT = "postgres"

# Node types that indicate the statement (or a CTE/subquery within it) would
# mutate state, change schema, or otherwise fall outside "read a value".
# Checked across the *entire* tree, not just the top-level node, because
# Postgres allows data-modifying statements inside a CTE of an overall
# SELECT (`WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`).
_DISALLOWED_NODE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Grant,
    exp.Command,
    exp.Into,
    exp.Copy,
    exp.Lock,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Set,
)

# Admin/IO/side-effecting functions that have no place in a read-only RAG
# query, even though they are syntactically valid inside a SELECT.
DANGEROUS_FUNCTIONS = {
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_ls_logdir", "pg_ls_waldir",
    "pg_ls_tmpdir", "pg_stat_file",
    "lo_import", "lo_export", "lo_get", "lo_put", "lo_from_bytea", "lo_creat", "lo_create",
    "dblink", "dblink_connect", "dblink_exec", "dblink_open", "dblink_send_query",
    "pg_read_server_files", "pg_write_server_files", "pg_execute_server_program",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile",
    "set_config", "pg_advisory_lock", "pg_advisory_lock_shared", "pg_advisory_xact_lock",
    "xpath", "xpath_exists", "query_to_xml", "query_to_xmlschema", "pg_promote",
}

# Schemas that are always off-limits, even for tables the permissions file
# doesn't otherwise mention by qualified name (system catalogs can leak
# credentials, other databases' data, or server internals).
_DISALLOWED_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}

_PLACEHOLDER_RE = re.compile(r"%s")


@dataclass
class ValidatedQuery:
    execution_sql: str
    tables: list[str]


def _to_parseable(sql: str) -> str:
    """Rewrite psycopg's `%s` placeholders as named params sqlglot can parse.

    Only used to build the AST for validation; the original text (with the
    real `%s` placeholders) is what actually gets executed.
    """

    counter = itertools.count(1)
    return _PLACEHOLDER_RE.sub(lambda _m: f":p{next(counter)}", sql)


def _select_depth(root: exp.Expression) -> int:
    max_depth = 0
    for select in root.find_all(exp.Select):
        depth = 0
        node = select
        while node is not None:
            if isinstance(node, exp.Select):
                depth += 1
            node = node.parent
        max_depth = max(max_depth, depth)
    return max_depth


class SqlValidator:
    def __init__(self, settings: Settings, permissions: PermissionsConfig, db: Database) -> None:
        self._settings = settings
        self._permissions = permissions
        self._db = db

    def validate(self, sql: str, parameters: list) -> ValidatedQuery:
        settings = self._settings

        if not sql or not sql.strip():
            raise ValidationError("SQL statement must not be empty.")
        if len(sql) > settings.max_query_length:
            raise QueryError(
                f"SQL statement exceeds the maximum allowed length ({settings.max_query_length} characters)."
            )

        sql_text = sql.strip()
        if sql_text.endswith(";"):
            sql_text = sql_text[:-1].strip()

        placeholder_count = len(_PLACEHOLDER_RE.findall(sql_text))
        if placeholder_count != len(parameters):
            raise ValidationError(
                f"Parameter count mismatch: SQL has {placeholder_count} placeholder(s) but "
                f"{len(parameters)} parameter(s) were supplied."
            )

        try:
            statements = [s for s in sqlglot.parse(_to_parseable(sql_text), read=DIALECT) if s is not None]
        except Exception as exc:  # sqlglot raises its own ParseError subclasses
            raise QueryError("SQL statement could not be parsed.") from exc

        if len(statements) != 1:
            raise QueryError("Only a single SQL statement is allowed per call.")

        root = statements[0]

        top_level_select = root
        if isinstance(root, exp.With):
            top_level_select = root.this
        if not isinstance(top_level_select, (exp.Select, exp.Union)):
            raise QueryError("Only read-only SELECT statements (optionally with CTEs) are allowed.")

        for node_type in _DISALLOWED_NODE_TYPES:
            if root.find(node_type) is not None:
                raise QueryError(f"Statement contains a disallowed construct: {node_type.__name__}.")

        self._check_dangerous_functions(root)
        tables_by_alias, table_names = self._check_tables(root)
        self._check_columns(root, tables_by_alias, table_names)
        self._check_complexity(root)

        wrapped = (
            f"SELECT * FROM ({sql_text}) AS _db_mcp_validated LIMIT {settings.max_query_rows + 1}"
        )
        return ValidatedQuery(execution_sql=wrapped, tables=sorted(table_names))

    def _check_dangerous_functions(self, root: exp.Expression) -> None:
        for func in root.find_all((exp.Func, exp.Anonymous)):
            name = (func.name or type(func).__name__).lower()
            if name in DANGEROUS_FUNCTIONS:
                raise QueryError(f"Use of function '{name}' is not permitted.")

    def _check_tables(self, root: exp.Expression) -> tuple[dict[str, str], set[str]]:
        alias_to_table: dict[str, str] = {}
        table_names: set[str] = set()

        for table in root.find_all(exp.Table):
            schema = (table.db or "").lower()
            if schema and schema in _DISALLOWED_SCHEMAS:
                raise AuthorizationError(f"Access to schema '{schema}' is not permitted.")
            if schema and schema != "public":
                raise AuthorizationError(f"Access to schema '{schema}' is not permitted; only 'public' is allowed.")

            name = table.name
            if not name:
                continue
            # Every object this server creates is stored lowercase (see
            # identifier_validator.validate_identifier); replicate Postgres'
            # own case-folding here so an unquoted `FROM Foo` resolves the
            # same way it will when the wrapped SQL actually executes. A
            # quoted `FROM "Foo"` is left as-is, exactly like Postgres.
            if not table.this.args.get("quoted"):
                name = name.lower()
            table_names.add(name)
            alias_to_table[table.alias_or_name] = name

        if len(table_names) > self._settings.max_tables_per_query:
            raise QueryError(
                f"Query references {len(table_names)} tables, exceeding the limit of "
                f"{self._settings.max_tables_per_query}."
            )

        for table in table_names:
            if not self._db.table_exists(table):
                raise ValidationError(f"Table '{table}' does not exist.")
            self._permissions.check(table, "select")

        return alias_to_table, table_names

    def _check_columns(
        self,
        root: exp.Expression,
        alias_to_table: dict[str, str],
        table_names: set[str],
    ) -> None:
        deny_by_table = {
            t: set(self._permissions.tables[t].deny_columns)
            for t in table_names
            if t in self._permissions.tables
        }
        any_denied_columns = any(deny_by_table.values())

        if any_denied_columns and root.find(exp.Star) is not None:
            raise AuthorizationError(
                "SELECT * is not permitted when a referenced table restricts specific columns; "
                "select the allowed columns explicitly."
            )

        for column in root.find_all(exp.Column):
            col_name = column.name
            if not column.this.args.get("quoted"):
                col_name = col_name.lower()
            table_ref = column.table
            if table_ref:
                real_table = alias_to_table.get(table_ref, table_ref)
                if real_table in deny_by_table and col_name in deny_by_table[real_table]:
                    raise AuthorizationError(
                        f"Access to column '{col_name}' on table '{real_table}' is denied."
                    )
            else:
                for table, denied in deny_by_table.items():
                    if col_name in denied:
                        raise AuthorizationError(
                            f"Access to column '{col_name}' is denied on table '{table}'; "
                            "qualify the column reference to disambiguate if this was a different table."
                        )

    def _check_complexity(self, root: exp.Expression) -> None:
        settings = self._settings

        join_count = len(list(root.find_all(exp.Join)))
        if join_count > settings.max_joins_per_query:
            raise QueryError(f"Query has {join_count} joins, exceeding the limit of {settings.max_joins_per_query}.")

        cte_count = len(list(root.find_all(exp.CTE)))
        if cte_count > settings.max_cte_count:
            raise QueryError(f"Query has {cte_count} CTEs, exceeding the limit of {settings.max_cte_count}.")

        depth = _select_depth(root)
        if depth > settings.max_subquery_depth:
            raise QueryError(
                f"Query nesting depth {depth} exceeds the limit of {settings.max_subquery_depth}."
            )
