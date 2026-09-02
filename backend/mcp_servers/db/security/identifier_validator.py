"""Validation and safe quoting for SQL identifiers (tables, columns, indexes, constraints).

SQL parameters (`%s` placeholders) can only ever bind to *values*. Table and
column names must be embedded directly into the generated SQL text, so they
are the one place a naive implementation could reintroduce injection. Every
identifier that reaches this module is validated against a strict allowlist
pattern and against the PostgreSQL reserved-word list before being quoted
with double quotes (escaping any embedded quote), which is what makes it
safe to interpolate.
"""

from __future__ import annotations

import re

from backend.mcp_servers.db.errors import ValidationError

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")

# Not exhaustive, but covers the reserved words most likely to be handed to us
# by mistake or maliciously (keywords that would change statement structure
# if used unquoted, or that are suspicious in an identifier position).
_RESERVED_WORDS = {
    "select", "insert", "update", "delete", "drop", "alter", "create",
    "truncate", "grant", "revoke", "union", "where", "from", "join",
    "table", "database", "schema", "into", "values", "set", "and", "or",
    "not", "null", "true", "false", "as", "on", "in", "is", "like",
    "between", "exists", "case", "when", "then", "else", "end", "cast",
    "primary", "foreign", "key", "references", "constraint", "default",
    "check", "unique", "index", "view", "trigger", "function", "procedure",
    "with", "order", "group", "having", "limit", "offset", "distinct",
    "all", "any", "commit", "rollback", "transaction", "begin", "execute",
    "user", "current_user", "session_user", "public",
}


def validate_identifier(name: str, *, kind: str = "identifier") -> str:
    """Validate a raw identifier string. Returns it lowercased if valid.

    Every identifier this server creates or references is normalized to
    lowercase, matching how Postgres folds *unquoted* identifiers by default.
    Since our own DDL always quotes what this returns, an object we create as
    "CamelCase" would otherwise only be reachable via a quoted reference —
    silently breaking plain `SELECT * FROM CamelCase` in query_data (which
    Postgres folds to lowercase, the same as everywhere else). Normalizing
    here keeps quoted-DDL and unquoted-raw-SQL identifier resolution
    consistent everywhere in the server.

    Raises ValidationError for anything that isn't a plain, safely-quotable
    SQL identifier: empty strings, identifiers over 63 bytes (Postgres' own
    limit), embedded quotes/whitespace/punctuation, or reserved words.
    """

    if not isinstance(name, str) or not name:
        raise ValidationError(f"Invalid {kind}: must be a non-empty string.")

    if not _IDENTIFIER_RE.match(name):
        raise ValidationError(
            f"Invalid {kind} '{name}': must start with a letter or underscore and contain "
            "only letters, digits, and underscores (max 63 characters)."
        )

    if name.lower() in _RESERVED_WORDS:
        raise ValidationError(f"Invalid {kind} '{name}': '{name}' is a reserved SQL keyword.")

    return name.lower()


def quote_identifier(name: str, *, kind: str = "identifier") -> str:
    """Validate then double-quote an identifier for safe interpolation into SQL text."""

    validated = validate_identifier(name, kind=kind)
    # Defense in depth: even though the allowlist regex already excludes it,
    # never trust a single validation layer with identifier interpolation.
    escaped = validated.replace('"', '""')
    return f'"{escaped}"'


def quote_qualified(*parts: str, kind: str = "identifier") -> str:
    """Quote a dotted identifier path (e.g. schema.table) part by part."""

    return ".".join(quote_identifier(part, kind=kind) for part in parts)
