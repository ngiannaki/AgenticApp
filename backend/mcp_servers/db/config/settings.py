"""Runtime configuration for the Database MCP server.

All values are configurable through environment variables (optionally loaded
from a `.env` file next to this package) so that limits and safety switches
never need to be hard-coded into tool logic. See `.env.example` for the full
list with defaults and explanations.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PACKAGE_DIR / ".env"),
        env_file_encoding="utf-8",
        env_prefix="DB_MCP_",
        extra="ignore",
    )

    # --- Connectivity -----------------------------------------------------
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/postgres",
        description="PostgreSQL connection string (libpq/psycopg URI).",
    )
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=10, ge=1)

    # --- Server -------------------------------------------------------------
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8002)

    # --- Query safety limits (query_data) -----------------------------------
    max_query_rows: int = Field(default=1000, gt=0, description="Hard cap on rows returned by query_data.")
    max_query_length: int = Field(default=10_000, gt=0, description="Max characters accepted in a raw SQL string.")
    query_timeout_seconds: int = Field(default=10, gt=0, description="Postgres statement_timeout for query_data.")
    max_result_bytes: int = Field(default=5_000_000, gt=0, description="Approximate cap on serialized result size.")
    max_tables_per_query: int = Field(default=10, gt=0)
    max_joins_per_query: int = Field(default=8, gt=0)
    max_cte_count: int = Field(default=5, gt=0)
    max_subquery_depth: int = Field(default=4, gt=0)

    # --- Mutation / schema safety switches ----------------------------------
    allow_schema_changes: bool = Field(default=True, description="Master switch for create_table/update_table_definition.")
    allow_delete: bool = Field(default=True, description="Master switch for delete_data.")
    allow_full_table_delete: bool = Field(default=False, description="Allow delete_data without a filter, given explicit opt-in flag on the call.")
    allow_destructive_schema_ops: bool = Field(
        default=False,
        description="Master switch for destructive update_table_definition operations (drop_column, drop_constraint, drop_table via rename tricks, etc.) even when the caller passes confirm=true.",
    )
    mutation_timeout_seconds: int = Field(default=30, gt=0, description="Postgres statement_timeout for insert/update/delete/schema statements.")
    max_batch_insert_records: int = Field(default=1000, gt=0)

    # --- Authorization -------------------------------------------------------
    permissions_file: str = Field(
        default=str(_PACKAGE_DIR / "permissions.yaml"),
        description="Path to the YAML permissions/protected-tables configuration.",
    )

    # --- Logging ---------------------------------------------------------------
    log_dir: str = Field(default="logs")
    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
