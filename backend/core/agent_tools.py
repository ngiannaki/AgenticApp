"""Capabilities a user-built agent's skills can grant: each group is a set
of the existing model tools (backend/config/settings.py API_MODEL_TOOLS).
Shared by the API (validation, the builder UI) and the agents service (the
generic custom-agent runner). Every call still goes through the API's
execute_tool: allowlist, document ownership, mandatory approvals."""
from __future__ import annotations

TOOL_GROUPS: dict[str, dict] = {
    "documents": {
        "label": "Your documents",
        "description": "Search and read your uploaded documents, with page citations.",
        "tools": [
            "retrieval_search_my_documents_v2",
            "retrieval_retrieve_document_v2",
            "retrieval_get_table_of_contents_v2",
            "retrieval_get_document_info_v2",
        ],
    },
    "web": {
        "label": "Web search",
        "description": "Search the web and read pages.",
        "tools": ["retrieval_web_search", "retrieval_fetch_page"],
    },
    "database": {
        "label": "Database",
        "description": "Query your private tables. Creating or changing data asks you first.",
        "tools": [
            "db_list_tables",
            "db_describe_table",
            "db_query_data",
            "db_create_table",
            "db_insert_data",
            "db_update_data",
            "db_delete_data",
        ],
    },
    "code": {
        "label": "Python code",
        "description": "Run Python in the sandbox: analyse spreadsheets, compute, make charts and files.",
        "tools": [
            "code_run_python_code",
            "code_profile_spreadsheet",
            "code_run_python_code_on_document",
        ],
    },
    "reports": {
        "label": "Reports & files",
        "description": "Create formatted Word, Excel, Markdown or CSV reports to download.",
        "tools": ["code_generate_file"],
    },
    "ui": {
        "label": "Interactive UI",
        "description": "Build interactive pages — dashboards, calculators, forms — shown live in the result.",
        "tools": ["code_generate_ui"],
    },
}

ALL_GROUP_TOOLS: set[str] = {t for g in TOOL_GROUPS.values() for t in g["tools"]}


def tools_for(groups: list[str]) -> list[str]:
    return sorted({t for g in groups if g in TOOL_GROUPS for t in TOOL_GROUPS[g]["tools"]})
