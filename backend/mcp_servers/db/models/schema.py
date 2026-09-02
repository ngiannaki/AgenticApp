"""Structured request models for `create_table` and `update_table_definition`.

These are the only shapes the schema tools accept — there is no path from
client input to raw ALTER/CREATE SQL text.
"""

from __future__ import annotations

from typing import Literal, Optional

from typing import Annotated

from pydantic import BaseModel, Field, model_validator

# Deliberately a small, closed vocabulary rather than "any type Postgres
# supports" — an allowlist keeps create_table from being usable to sneak in
# exotic/dangerous types, and keeps the type surface predictable for agents.
# This is a PostgreSQL-backed server: these are our own logical type names,
# NOT Postgres/SQL type names (no TEXT, VARCHAR, REAL, etc.) — each maps to
# a specific Postgres type below.
ALLOWED_COLUMN_TYPES: dict[str, str] = {
    "STRING": "TEXT",
    "FLOAT": "DOUBLE PRECISION",
    "INTEGER": "INTEGER",
    "DATETIME": "TIMESTAMP",
    "BOOLEAN": "BOOLEAN",
}

# Exposed as a real Literal (not a free `str` + post-hoc check) so the JSON
# schema handed to MCP clients declares an explicit `enum` for this field —
# the client's tool-calling layer can validate/constrain against it directly
# instead of guessing a SQL type name and failing after the fact.
ColumnType = Literal["STRING", "FLOAT", "INTEGER", "DATETIME", "BOOLEAN"]

OnExists = Literal["fail", "ignore"]
IfExistsAction = Literal["error", "skip"]


class ForeignKeySpec(BaseModel):
    ref_table: str
    ref_column: str
    on_delete: Literal["CASCADE", "RESTRICT", "SET NULL", "NO ACTION"] = "NO ACTION"


class ColumnDef(BaseModel):
    name: str
    type: ColumnType
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    default: Optional[str] = None
    foreign_key: Optional[ForeignKeySpec] = None


class IndexDef(BaseModel):
    name: str
    columns: list[str] = Field(min_length=1)
    unique: bool = False


class CreateTableRequest(BaseModel):
    table: str
    columns: list[ColumnDef] = Field(min_length=1)
    indexes: list[IndexDef] = Field(default_factory=list)
    if_exists: OnExists = "fail"

    @model_validator(mode="after")
    def _check_single_primary_key_group(self) -> "CreateTableRequest":
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate column names in create_table request.")
        return self


# --- update_table_definition operations -------------------------------------


class AddColumnOp(BaseModel):
    operation: Literal["add_column"]
    column: ColumnDef


class DropColumnOp(BaseModel):
    operation: Literal["drop_column"]
    name: str
    confirm: bool = False


class RenameColumnOp(BaseModel):
    operation: Literal["rename_column"]
    old_name: str
    new_name: str


class AlterColumnTypeOp(BaseModel):
    operation: Literal["alter_column_type"]
    name: str
    new_type: ColumnType
    confirm: bool = False


class AlterColumnNullableOp(BaseModel):
    operation: Literal["alter_column_nullable"]
    name: str
    nullable: bool


class AddUniqueConstraintOp(BaseModel):
    operation: Literal["add_unique_constraint"]
    name: str
    columns: list[str] = Field(min_length=1)


class AddForeignKeyOp(BaseModel):
    operation: Literal["add_foreign_key"]
    name: str
    column: str
    foreign_key: ForeignKeySpec


class DropConstraintOp(BaseModel):
    operation: Literal["drop_constraint"]
    name: str
    confirm: bool = False


class AddIndexOp(BaseModel):
    operation: Literal["add_index"]
    name: str
    columns: list[str] = Field(min_length=1)
    unique: bool = False


class DropIndexOp(BaseModel):
    operation: Literal["drop_index"]
    name: str
    confirm: bool = False


class RenameTableOp(BaseModel):
    operation: Literal["rename_table"]
    new_name: str


SchemaOperation = Annotated[
    AddColumnOp
    | DropColumnOp
    | RenameColumnOp
    | AlterColumnTypeOp
    | AlterColumnNullableOp
    | AddUniqueConstraintOp
    | AddForeignKeyOp
    | DropConstraintOp
    | AddIndexOp
    | DropIndexOp
    | RenameTableOp,
    Field(discriminator="operation"),
]

# Operations that destroy data or drop structure outright; require confirm=true
# AND the server-wide allow_destructive_schema_ops switch.
DESTRUCTIVE_OPERATIONS = {"drop_column", "alter_column_type", "drop_constraint", "drop_index"}


class UpdateTableDefinitionRequest(BaseModel):
    table: str
    operations: list[SchemaOperation] = Field(min_length=1)
