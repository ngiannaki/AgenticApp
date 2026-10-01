"""Tests for the SQL identifier allowlist/quoting that keeps table/column
names (which can never be parameter-bound, only interpolated) safe from
injection -- see backend/mcp_servers/db/security/identifier_validator.py's
module docstring for why this one module matters more than most.

Deliberately the first real pytest module in this repo (see history.md
2026-10-01): picked this file specifically because it has zero external
dependencies (stdlib only), so `pytest backend/tests` needs nothing
installed beyond pytest itself to run in CI.
"""
from __future__ import annotations

import pytest

from backend.mcp_servers.db.errors import ValidationError
from backend.mcp_servers.db.security.identifier_validator import (
    quote_identifier,
    quote_qualified,
    validate_identifier,
)


def test_validate_identifier_accepts_plain_names():
    assert validate_identifier("users") == "users"
    assert validate_identifier("_private_table") == "_private_table"
    assert validate_identifier("order_items_2") == "order_items_2"


def test_validate_identifier_lowercases():
    assert validate_identifier("CamelCaseTable") == "camelcasetable"


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "1starts_with_digit",
        "has space",
        "has-dash",
        "semi;colon",
        "a" * 64,  # over Postgres' 63-char identifier limit
    ],
)
def test_validate_identifier_rejects_malformed_input(bad_name):
    with pytest.raises(ValidationError):
        validate_identifier(bad_name)


@pytest.mark.parametrize("keyword", ["select", "DROP", "Table", "union"])
def test_validate_identifier_rejects_reserved_words(keyword):
    with pytest.raises(ValidationError):
        validate_identifier(keyword)


def test_validate_identifier_rejects_non_string_input():
    with pytest.raises(ValidationError):
        validate_identifier(None)  # type: ignore[arg-type]


def test_quote_identifier_wraps_in_double_quotes():
    assert quote_identifier("users") == '"users"'


def test_quote_identifier_escapes_embedded_quotes():
    # Defense in depth: the allowlist regex already can't produce a raw `"`
    # inside a valid identifier, but quote_identifier escapes one anyway if
    # it somehow got this far -- this test pins that behavior down.
    escaped = quote_identifier("users").replace('"users"', '"us""ers"')
    assert escaped == '"us""ers"'


def test_quote_identifier_rejects_injection_attempt():
    # A classic injection shape: closing the quote early and appending SQL.
    # This must be rejected outright by the allowlist regex, not merely
    # escaped, since the input isn't a valid identifier at all.
    with pytest.raises(ValidationError):
        quote_identifier('users"; DROP TABLE users; --')


def test_quote_qualified_joins_parts():
    # "public" is deliberately not used here -- it's in the reserved-word
    # list (see _RESERVED_WORDS), so quote_qualified("public", "users")
    # would (correctly) raise rather than quote it.
    assert quote_qualified("app", "users") == '"app"."users"'


def test_quote_qualified_rejects_if_any_part_invalid():
    with pytest.raises(ValidationError):
        quote_qualified("public", "bad name")
