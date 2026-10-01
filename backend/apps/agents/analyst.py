"""Data Analyst: answers a question about the user's spreadsheets and/or DB
tables with real pandas in the code sandbox; charts and result files come
back as deliverables; saving a result table needs the user's approval."""
from __future__ import annotations

import json
from typing import Any

from backend.apps.agents.base import BaseAgent, Field, RunContext

_SYSTEM = (
    "You are a careful data analyst. Use the tools to look at the data before computing anything: "
    "code_profile_spreadsheet for an attached spreadsheet, db_list_tables/db_describe_table for the user's tables. "
    "Compute answers with real code (code_run_python_code_on_document reads the spreadsheet from sys.argv[1]; "
    "db_query_data for SQL) — never guess numbers. Save charts with plt.savefig('name.png') and result tables "
    "with df.to_csv('name.csv', index=False); they are returned to the user automatically. "
    "{save_rule} Finish with a concise markdown summary of the findings, with the key numbers."
)
_SAVE_RULE_ON = "If the user would benefit from keeping a result table, call save_table (the user is asked to approve)."
_SAVE_RULE_OFF = "Do not save tables to the database."

_SAVE_TABLE_TOOL = {
    "type": "function",
    "function": {
        "name": "save_table",
        "description": "Save a result table into the user's private database (asks the user to approve first).",
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "lowercase table name"},
                "columns": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"name": {"type": "string"},
                              "type": {"type": "string", "enum": ["STRING", "INTEGER", "FLOAT", "BOOLEAN", "DATETIME"]}}},
                },
                "rows": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["table", "columns", "rows"],
        },
    },
}


class DataAnalystAgent(BaseAgent):
    id = "data_analyst"
    name = "Data Analyst"
    description = (
        "Analyses your spreadsheets and database tables with real code: exact numbers, "
        "charts and result files. Can save result tables (with your approval)."
    )
    inputs = [
        Field("question", "textarea", "What do you want to know?", required=True),
        Field("document_ids", "documents", "Spreadsheets", file_types=["csv", "xlsx", "xlsm"]),
        Field("allow_save", "boolean", "Allow saving result tables", default=False),
    ]
    allowed_tools = {
        "code_profile_spreadsheet", "code_run_python_code_on_document", "code_run_python_code",
        "db_list_tables", "db_describe_table", "db_query_data", "db_create_table", "db_insert_data",
    }
    examples = ["Which region grew fastest last quarter? Show a chart."]
    _loop_tools = allowed_tools - {"db_create_table", "db_insert_data"}

    async def run(self, ctx: RunContext, inputs: dict[str, Any]) -> None:
        files: list[dict] = []

        async def on_call(name: str, args: dict, ok: bool, text: str) -> None:
            data = _json(text)
            produced = [a for a in (data or {}).get("artifacts", []) if a.get("url")] if isinstance(data, dict) else []
            files.extend(produced)
            detail = args.get("code") or args.get("sql") or json.dumps(args)[:500]
            output = ""
            if isinstance(data, dict):
                output = (data.get("stdout") or data.get("stderr") or data.get("error") or "")[:4000]
            await ctx.step("code" if "code" in args else "tool", f"{name}{'' if ok else ' (failed)'}", detail,
                           {"output": output, "files": produced})

        async def save_table(args: dict) -> str:
            table, rows = str(args.get("table", "")), args.get("rows") or []
            answer = await ctx.ask(f"Save {len(rows)} rows as table '{table}' in your database?", ["Approve", "Reject"])
            if not answer.strip().lower().startswith("approve"):
                return "The user declined saving the table."
            created = await ctx.call_tool("db_create_table", {"table": table, "columns": args.get("columns") or [],
                                                              "if_exists": "ignore"}, approved=True)
            if not created.ok:
                return created.text
            inserted = await ctx.call_tool("db_insert_data", {"table": table, "records": rows, "returning": False}, approved=True)
            await ctx.step("result", f"Saved table '{table}'" if inserted.ok else "Saving failed", inserted.text[:300])
            return inserted.text

        local = {"save_table": (_SAVE_TABLE_TOOL, save_table)} if inputs["allow_save"] else {}
        docs = ", ".join(inputs["document_ids"]) or "none (use the database tables)"
        await ctx.step("plan", "Looking at the data")
        summary = await ctx.tool_loop(
            _SYSTEM.format(save_rule=_SAVE_RULE_ON if inputs["allow_save"] else _SAVE_RULE_OFF),
            f"Question: {inputs['question']}\nAttached spreadsheet document_ids: {docs}",
            self._loop_tools,
            local_tools=local,
            on_call=on_call,
        )
        await ctx.artifact("summary.md", text=summary or "No summary was produced.")
        if files:
            await ctx.artifact("files", data=files, kind="files")


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None
