"""Single source of truth for every setting used across the backend.

One `.env` file (`backend/.env`) backs all of it. Each concern gets its own
`BaseSettings` group (its own env var prefix, all reading the same file), and
`Settings` below composes them into one object — `from backend.config import
settings` is the only way any module should reach configuration; nothing
should read `os.environ` / hard-code a model name, URL, or path directly.

Environment switch
-------------------
`ENVIRONMENT=DEV` (default) or `ENVIRONMENT=PROD`. Every functionality that
calls an LLM (chat, embeddings, retrieval synthesis, the retrieval agent,
metadata enhancement) has its own `..._DEV` / `..._PROD` model pair, so each
can use a different model independently — but all of them are always reached
through the same `AsyncOpenAI`-compatible client: DEV points that client at
Ollama's OpenAI-compatible endpoint, PROD points it at real OpenAI. Call
sites never branch on environment themselves; they just ask `settings` for
the active model name and a client from `backend.config.llm_client`.
"""

from __future__ import annotations

import sys
from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parent.parent


def _blank_to_none(value: object) -> object:
    """An unset-but-present `KEY=` line in .env parses as `""`, not `None` —
    normalize that back to None so optional fields fall through to their
    real default instead of becoming an empty string."""
    return None if value == "" else value
_ENV_FILE = str(_BACKEND_DIR / ".env")


def _settings_config(prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix=prefix,
        extra="ignore",
    )


class Environment(str, Enum):
    DEV = "DEV"
    PROD = "PROD"


class _RootSettings(BaseSettings):
    model_config = _settings_config("")

    environment: Environment = Environment.DEV


class OllamaSettings(BaseSettings):
    """Used whenever ENVIRONMENT=DEV."""

    model_config = _settings_config("OLLAMA_")

    base_url: str = "http://localhost:11434"
    # Context window (in tokens) requested from Ollama for every DEV chat/completion
    # call. Ollama's own default is a mere 2048 tokens regardless of what the model
    # actually supports, which is why conversations "forget" earlier turns fast.
    # Native max context per model currently configured for DEV:
    #   - gpt-oss:20b        -> 131,072 tokens (128k)
    #   - qwen3-embedding:4b -> 32,768 tokens (embeddings don't use num_ctx the same
    #                           way, but this is its ceiling if ever used for chat)
    # Setting num_ctx above a model's native max has no effect beyond that max —
    # Ollama just clamps down to what the model actually supports. Also note a
    # bigger num_ctx means more VRAM/RAM used per request, so don't max it out
    # unless you actually need the extra history.
    num_ctx: int = 8192*3

    # Reasoning effort requested from thinking-capable models (e.g. gpt-oss:20b)
    # via Ollama's OpenAI-compatible endpoint. One of "low", "medium", "high",
    # or None to leave thinking at the model's own default. Ollama maps this to
    # its internal Think field and returns the model's chain-of-thought in the
    # response message's "reasoning" field.
    reasoning_effort: str | None = None  # "medium"


class OpenAISettings(BaseSettings):
    """Used whenever ENVIRONMENT=PROD."""

    model_config = _settings_config("OPENAI_")

    api_key: str | None = None
    base_url: str | None = Field(default=None, description="Override for Azure OpenAI or another OpenAI-compatible endpoint. None = api.openai.com.")

    # Reasoning effort for a reasoning-capable PROD model (e.g. an o-series or
    # "-thinking"/"-luna" style model): "low"/"medium"/"high"/"none", or blank
    # (the default) to send nothing. This MUST stay blank for a classic
    # non-reasoning model like gpt-4o-mini — OpenAI's /v1/chat/completions
    # rejects the reasoning_effort parameter outright for those with a 400
    # ("Unsupported parameter"), the same way it rejects it for a reasoning
    # model when tools are present without any value at all. Pair this with
    # CHAT_MODEL_PROD/RETRIEVAL_MODEL_PROD/etc. — set both together when
    # switching to a reasoning model, clear both together when switching back.
    reasoning_effort: str | None = None

    _normalize = field_validator("api_key", "base_url", "reasoning_effort", mode="before")(_blank_to_none)


class ChatModelSettings(BaseSettings):
    """Main chat agent — backend/apps/api."""

    model_config = _settings_config("CHAT_MODEL_")

    dev: str = "gpt-oss:20b"
    prod: str = "gpt-4o-mini"


class EmbeddingModelSettings(BaseSettings):
    """Shared by indexing and retrieval — both must embed into the same vector space."""

    model_config = _settings_config("EMBEDDING_MODEL_")

    dev: str = "qwen3-embedding:4b"
    prod: str = "text-embedding-3-small"


class RetrievalModelSettings(BaseSettings):
    """Answer-synthesis LLM used by the retrieval MCP server (backend/mcp_servers/retrieval)."""

    model_config = _settings_config("RETRIEVAL_MODEL_")

    dev: str = "gpt-oss:20b"
    prod: str = "gpt-4o-mini"
    temperature: float = 0.3


class MetadataModelSettings(BaseSettings):
    """Document summary/section extraction LLM (backend/infrastructure/vector_store/metadata_enhancer.py)."""

    model_config = _settings_config("METADATA_MODEL_")

    dev: str = "gpt-oss:20b"
    prod: str = "gpt-4o-mini"
    temperature: float = 0.1


class ApiSettings(BaseSettings):
    """The FastAPI SSE backend (backend/apps/api)."""

    model_config = _settings_config("API_")

    host: str = "0.0.0.0"
    port: int = 8100

    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000"],
        description="Origins allowed to call the API (the Next.js frontend).",
    )

    servers_path: Path = _BACKEND_DIR / "apps" / "client" / "servers.json"
    """Path to the FastMCP-style servers.json mapping used by example_mcp_client.py."""

    max_iterations: int = 15
    temperature: float = 0.3
    max_history_messages: int = 40

    max_tool_result_chars: int = 32_000
    """Hard cap on a single tool result's length before it enters conversation
    memory (and therefore gets resent to the LLM on every following turn).
    Tool authors are told to return a URL for large payloads (files, images)
    rather than inlining them, but this is a backstop for when a tool — or a
    model that ignores that instruction, e.g. base64-encoding an image into
    stdout instead of saving it — doesn't cooperate. Truncated, not the raw
    tool output shown to the user in the UI, which is unaffected.

    Was 8000 (~2K tokens) until a real production document's
    get_table_of_contents_v2 result (251 headings, 23,596 chars) got
    silently truncated well before the specific heading the model needed —
    it never saw the page number and burned 6 extra retrieval calls
    guessing instead. 8000 was sized against nothing in particular; 32_000
    (~8K tokens) covers that real payload with headroom for a larger filing
    while staying well under a single turn's context budget (DEV:
    OLLAMA_NUM_CTX minus ~4K reserved for generation — see
    sessions.py's _max_context_tokens) and still catching a genuinely
    pathological dump (a raw image/file inlined instead of returned as a
    URL, as tool authors are asked to do)."""

    log_level: str = "INFO"

    stream_tokens: bool = Field(default=True, description="Stream the model's reply token by token (`token` SSE events). false = whole reply at once.")

    daily_chat_turns: int = Field(default=0, description="Chat messages per user per day (0 = unlimited).")
    daily_uploads: int = Field(default=0, description="Document uploads per user per day (0 = unlimited).")
    daily_agent_tasks: int = Field(default=0, description="Agent tasks (incl. reruns) per user per day (0 = unlimited).")

    approval_required_tools: list[str] = Field(
        default_factory=list,
        description=(
            "Tool names that always pause for human approval before executing. "
            "Use ['*'] to require approval for every tool call by default. "
            "Sessions may override this at creation time — but only on top of "
            "mandatory_approval_tools below, which no session can opt out of."
        ),
    )

    mandatory_approval_tools: list[str] = Field(
        default_factory=lambda: [
            "db_create_table",
            "db_update_table_definition",
            "db_insert_data",
            "db_update_data",
            "db_delete_data",
        ],
        description=(
            "Tools that always pause for human approval, regardless of what a "
            "session asked for at creation time. Server-side policy: a client can "
            "add tools to its session's approval list but never remove these."
        ),
    )

    model_tools: list[str] = Field(
        default_factory=lambda: [
            "retrieval_retrieve_document_v2",
            "retrieval_get_table_of_contents_v2",
            "retrieval_get_document_info_v2",
            "retrieval_search_my_documents_v2",
            "retrieval_web_search",
            "retrieval_fetch_page",
            "db_list_tables",
            "db_describe_table",
            "db_create_table",
            "db_update_table_definition",
            "db_insert_data",
            "db_update_data",
            "db_delete_data",
            "db_query_data",
            "code_run_python_code",
            "code_check_python_syntax",
            "code_profile_spreadsheet",
            "code_run_python_code_on_document",
            "code_generate_file",
            "code_generate_ui",
        ],
        description=(
            "Allowlist of MCP tools (aggregated '<server>_<tool>' names) the chat "
            "model is shown and may call. Anything else an MCP server registers "
            "stays callable by the API itself (routers/files.py's upload/delete/"
            "chunk-listing calls) but is invisible to the model — so a new tool, "
            "or one taking a server-local file_path (indexing_upload_document), "
            "never becomes model-reachable by accident. Use ['*'] to expose every "
            "tool (local single-user debugging only)."
        ),
    )

    tool_call_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description="Hard ceiling on a single model-initiated MCP tool call before it's reported back to the model as a timeout.",
    )

    interrupt_heartbeat_seconds: float = 15.0
    """How often to send an SSE keep-alive comment while waiting on a human decision."""

    interrupt_timeout_seconds: float | None = None
    """Optional hard timeout for a pending human-in-the-loop decision. None = wait forever."""

    session_idle_timeout_seconds: float = 3600.0
    """Sessions untouched for this long are evicted by the periodic cleanup task."""

    skills_enabled: bool = Field(
        default=True,
        description=(
            "Feature flag for the agent-skills wiki (backend/skills/): when true, the "
            "chat agent gets two extra local tools, list_skills and get_skill, that let "
            "it discover and read markdown skill files on demand (Anthropic Agent Skills "
            "format, agentskills.io). Off by default so this is purely additive — "
            "existing tool-calling behavior is unchanged until explicitly enabled."
        ),
    )
    skills_dir: Path = _BACKEND_DIR / "skills"
    """Directory of skill folders (each `<skills_dir>/<name>/SKILL.md`) the agent can list/read when skills_enabled is true."""


class DbMcpSettings(BaseSettings):
    """Runtime configuration for the Database MCP server (backend/mcp_servers/db)."""

    model_config = _settings_config("DB_MCP_")

    # --- Connectivity -----------------------------------------------------
    database_url: str = Field(
        default="postgresql://agent_ws:changeme-agent@localhost:5432/ragdb",
        description=(
            "PostgreSQL connection string for the DB MCP server. Connect as the "
            "restricted agent role (DATABASE_AGENT_ROLE), never the app's own role: "
            "the agent role can't see the app/rag schemas, only the per-user "
            "ws_<hash> workspace schemas this server creates."
        ),
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
        default=str(_BACKEND_DIR / "mcp_servers" / "db" / "permissions.yaml"),
        description="Path to the YAML permissions/protected-tables configuration.",
    )

    # --- Logging ---------------------------------------------------------------
    log_dir: str = Field(default="logs")
    log_level: str = Field(default="INFO")


class CodeMcpSettings(BaseSettings):
    """Runtime configuration for the Code Execution MCP server (backend/mcp_servers/code)."""

    model_config = _settings_config("CODE_MCP_")

    # --- Server ---------------------------------------------------------------
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8003)

    # --- Execution --------------------------------------------------------------
    python_executable: str | None = Field(
        default=None,
        description="Interpreter used to run submitted code. Leave unset to use the interpreter running this server (the project's venv), resolved at settings load time.",
    )

    _normalize_python_executable = field_validator("python_executable", mode="before")(_blank_to_none)
    _normalize_file_source_url = field_validator("file_source_url", mode="before")(_blank_to_none)

    @field_validator("python_executable", mode="after")
    @classmethod
    def _default_python_executable(cls, value: str | None) -> str:
        return value or sys.executable
    default_timeout_seconds: int = Field(default=10, gt=0, description="Timeout applied when a call doesn't specify one.")
    max_timeout_seconds: int = Field(default=60, gt=0, description="Hard ceiling on any per-call timeout override.")
    max_code_length: int = Field(default=50_000, gt=0, description="Max characters accepted as submitted code/command text.")
    max_output_bytes: int = Field(default=200_000, gt=0, description="Combined stdout/stderr is truncated beyond this many bytes.")

    workspace_dir: str = Field(
        default=str(_BACKEND_DIR / "data" / "code_sandbox"),
        description="Parent directory for the isolated per-run working directories.",
    )
    keep_workspace: bool = Field(default=False, description="Keep each run's temp directory on disk after completion (debugging only). Off by default.")

    env_passthrough: list[str] = Field(
        default_factory=lambda: ["PATH", "SYSTEMROOT", "TEMP", "TMP", "PATHEXT", "USERPROFILE", "HOME"],
        description="Host environment variables forwarded into the executed subprocess. Everything else (API keys, DB URLs, ...) is stripped so submitted code can't read backend secrets via os.environ. USERPROFILE/HOME are included so libraries that cache to a home-relative dir (e.g. matplotlib's font cache) don't rebuild it on every run.",
    )

    # --- OS-level isolation (Linux; see execution/sandbox.py) ---------------------
    isolation: str = Field(
        default="auto",
        description=(
            "'uid': run each submission as its own unprivileged user (one of the runner "
            "uids below, never shared by two concurrent runs) with resource limits — needs "
            "this server to run as root on Linux, as it does in the code-sandbox container. "
            "'none': run as this server's own user (local Windows/dev only). 'auto': 'uid' "
            "when possible, else 'none' with a warning."
        ),
    )
    require_isolation: bool = Field(
        default=False,
        description="Refuse to run any code unless uid isolation is active. Set in the code-sandbox container so a misconfiguration fails closed.",
    )
    runner_uid_start: int = Field(default=20001, gt=0, description="First uid of the runner pool.")
    runner_uid_count: int = Field(default=8, gt=0, description="Runner pool size = max concurrent code runs.")
    runner_wait_seconds: float = Field(default=30.0, ge=0, description="How long a run waits for a free runner before failing as busy.")
    max_memory_mb: int = Field(default=2048, gt=0, description="Address-space limit per run (RLIMIT_AS).")
    max_processes: int = Field(default=64, gt=0, description="Process/thread limit per run (RLIMIT_NPROC, uid isolation only).")
    max_file_size_mb: int = Field(default=200, gt=0, description="Largest file a run may write (RLIMIT_FSIZE).")

    file_source_url: str | None = Field(
        default=None,
        description=(
            "Where spreadsheet tools fetch an attached document from: the API's internal "
            "one-time file endpoint (e.g. http://app:8100/internal/files). The API puts a "
            "single-use `file_token` in each call; this server never sees the shared "
            "document folder. Unset = read from the local document store (non-container dev)."
        ),
    )

    # --- Generated-file capture (plots, CSVs, ...) --------------------------------
    capture_artifacts: bool = Field(
        default=True,
        description="After a run, save any files the code wrote into its sandbox directory (besides script.py) so they can be served back to the UI — e.g. a matplotlib plot saved with plt.savefig(...).",
    )
    max_artifact_bytes: int = Field(default=10_000_000, gt=0, description="Per-file cap on a captured artifact's size; larger files are skipped, not truncated.")
    max_artifacts_per_run: int = Field(default=5, gt=0, description="Cap on how many generated files a single run can hand back.")
    artifact_public_base_url: str = Field(
        default="http://localhost:8100",
        description="Base URL the Agentic API is reachable at, used to build the URL returned for a captured artifact (.../api/files/{id}/{filename}). Point this at wherever the API actually listens (e.g. through nginx) in non-default deployments.",
    )

    # --- Shell command tool (off by default) -------------------------------------
    allow_shell_commands: bool = Field(
        default=False,
        description="Master switch for the run_shell_command tool. Off by default — arbitrary shell access is a much larger blast radius than sandboxed Python execution.",
    )
    allowed_shell_executables: list[str] = Field(
        default_factory=lambda: ["python", "pip", "pytest"],
        description="When allow_shell_commands is true, only commands whose first token matches one of these (case-insensitive, extension-stripped) are permitted.",
    )

    # --- Generative UI tool (on by default; the one operator-facing kill switch) --
    allow_ui_generation: bool = Field(
        default=True,
        description="Master switch for the generate_ui tool. When false, the tool isn't registered with the MCP server at all, so the model never sees it and can't call it — turn off here to stop generative-UI usage instantly without touching the frontend or the model's system prompt.",
    )
    max_ui_html_length: int = Field(
        default=200_000, gt=0,
        description="Max characters accepted as the generate_ui tool's html input.",
    )

    # --- Logging -------------------------------------------------------------------
    log_dir: str = Field(default="logs")
    log_level: str = Field(default="INFO")


class IndexingServerSettings(BaseSettings):
    """Networking for the indexing MCP server (backend/mcp_servers/indexing)."""

    model_config = _settings_config("INDEXING_MCP_")

    host: str = "0.0.0.0"
    port: int = 8000


class RetrievalServerSettings(BaseSettings):
    """Networking for the retrieval MCP server (backend/mcp_servers/retrieval)."""

    model_config = _settings_config("RETRIEVAL_MCP_")

    host: str = "0.0.0.0"
    port: int = 8001

    # --- Web search (web_search / fetch_page tools) --------------------------
    web_search_backend: str = Field(
        default="duckduckgo",
        description="Search backend for web_search. Only 'duckduckgo' is implemented (no API key required).",
    )
    web_search_region: str = Field(default="wt-wt", description="DuckDuckGo region code, e.g. 'us-en'. 'wt-wt' = no region bias.")
    web_search_safesearch: str = Field(default="moderate", description="One of 'on', 'moderate', 'off'.")
    web_search_default_max_results: int = Field(default=5, gt=0, description="Results returned when web_search doesn't specify max_results.")
    web_search_max_results_ceiling: int = Field(default=10, gt=0, description="Hard cap on web_search's max_results, regardless of what the caller asks for.")
    web_search_max_query_length: int = Field(default=400, gt=0, description="Max characters accepted in a web_search query string.")
    web_search_timeout_seconds: int = Field(default=10, gt=0, description="Timeout for the outbound HTTP request to the search backend.")

    fetch_page_timeout_seconds: int = Field(default=10, gt=0, description="Timeout for the outbound HTTP request in fetch_page.")
    fetch_page_max_content_chars: int = Field(default=8_000, gt=0, description="Extracted page text is truncated beyond this many characters.")
    fetch_page_max_response_bytes: int = Field(default=5_000_000, gt=0, description="Pages larger than this are rejected before extraction, to bound memory use.")
    fetch_page_allowed_schemes: list[str] = Field(
        default_factory=lambda: ["http", "https"],
        description="URL schemes fetch_page will follow. Blocks file://, data://, etc.",
    )


class DatabaseSettings(BaseSettings):
    """The one Postgres (with pgvector) behind everything: users, auth
    tokens, conversations, the document registry (schema `app`), document
    chunks + embeddings (schema `rag`), and each user's private DB-tool
    workspace (`ws_<hash>` schemas, reached only through the DB MCP server's
    own restricted role — see DbMcpSettings.database_url).

    Used by the API and the indexing/retrieval MCP servers. The API applies
    backend/migrations/*.sql at startup (see backend/core/db/migrate.py).
    """

    model_config = _settings_config("DATABASE_")

    url: str = "postgresql://raguser:changeme@localhost:5432/ragdb"
    """Connection string for the app's own role. Must be able to CREATE
    schemas/extensions on first start (the compose/ps1 Postgres container's
    POSTGRES_USER is)."""

    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=10, gt=0)

    agent_role: str = "agent_ws"
    """Login role the DB MCP server connects as. Has no access to the `app`
    or `rag` schemas — only to the per-user workspace schemas it creates."""

    agent_role_password: str | None = None
    """When set, API startup creates/updates `agent_role` with this password
    (idempotent). Must match the password in DB_MCP_DATABASE_URL. Leave unset
    to manage that role yourself."""


class DocumentStoreSettings(BaseSettings):
    model_config = _settings_config("DOCUMENT_STORE_")

    dir: str = "data/documents"


class ArtifactStoreSettings(BaseSettings):
    """Files generated by tool calls (currently: run_python_code output files
    like plots) and served back to the UI via GET /api/files/{id}/{filename}."""

    model_config = _settings_config("ARTIFACT_STORE_")

    dir: str = "data/artifacts"


class RerankingSettings(BaseSettings):
    model_config = _settings_config("RERANK_")

    retrieval_threshold: float = Field(default=0.1, description="Relevance threshold used by the retrieval MCP server's reranker.")


class LangchainRagSettings(BaseSettings):
    """Config for the LangChain-based v2 indexing/retrieval tools
    (backend/mcp_servers/indexing, retrieval `..._v2` tools), which store
    chunks + embeddings in Postgres (schema `rag`, see
    backend/core/vector_store/pg_store.py).
    """

    model_config = _settings_config("RAG_V2_")

    # --- Parsing (indexing) ---------------------------------------------------
    pdf_engine: str = Field(
        default="docling",
        description=(
            "Markdown-producing PDF engine for v2 indexing: 'docling' (layout-aware, "
            "richer table cell structure, heavier) or 'pymupdf4llm' (fast, dependency-light). "
            "Both preserve headings/tables as markdown; falls back to plain PyMuPDFLoader "
            "text if the selected engine's import/parse fails."
        ),
    )

    # --- Chunking (indexing) -------------------------------------------------
    chunk_size: int = Field(default=1000, gt=0, description="Target chunk size (characters) for RecursiveCharacterTextSplitter.")
    chunk_overlap: int = Field(default=150, ge=0, description="Overlap (characters) between adjacent chunks.")

    # --- Hybrid retrieval + reranking ----------------------------------------
    default_top_k: int = Field(default=8, gt=0, description="Chunks returned when the caller doesn't specify top_k.")
    max_top_k: int = Field(default=25, gt=0, description="Hard ceiling on top_k accepted from a caller.")
    fetch_k_multiplier: int = Field(default=6, gt=0, description="Each of the dense/sparse retrievers fetches top_k * this many candidates before ensembling/reranking.")
    max_fetch_k: int = Field(default=60, gt=0, description="Hard ceiling on the fetch_k computed above.")
    dense_weight: float = Field(default=0.5, ge=0, description="Reciprocal-rank-fusion weight for the dense (pgvector cosine) leg.")
    sparse_weight: float = Field(default=0.5, ge=0, description="Reciprocal-rank-fusion weight for the keyword (Postgres full-text) leg.")
    use_reranker: bool = Field(default=True, description="Rerank the ensembled candidates with a cross-encoder before truncating to top_k.")
    rerank_model: str = Field(default="cross-encoder/ms-marco-MiniLM-L-12-v2", description="HuggingFace cross-encoder model id used by the reranker.")

    # --- Grounding / citation context ----------------------------------------
    neighbor_expansion: bool = Field(default=True, description="Splice adjacent chunks (by chunk_index) around each returned hit for extra citation context.")
    neighbor_window: int = Field(default=1, ge=0, description="Number of adjacent chunks on each side to splice in when neighbor_expansion is on.")
    table_expand_max_chars: int = Field(default=8000, gt=0, description="A hit on part of a table split over several chunks returns the whole table, up to this many characters (parts nearest the hit first).")


class AgentsSettings(BaseSettings):
    """The A2A agents service (backend/apps/agents) and how the API reaches it."""

    model_config = _settings_config("AGENTS_")

    enabled: bool = True
    url: str = "http://localhost:9100"
    """Where the API reaches the agents service."""
    api_url: str = "http://localhost:8100"
    """Where the agents service reaches the API (internal tool endpoint)."""
    host: str = "127.0.0.1"
    port: int = 9100
    internal_token: str | None = None
    """Shared secret for agents <-> API internal calls. Required: agents are
    disabled until it's set (to the same value for both processes)."""
    max_active_tasks_per_user: int = Field(default=2, gt=0)
    tool_call_timeout_seconds: float = Field(default=300.0, gt=0)

    _normalize = field_validator("internal_token", mode="before")(_blank_to_none)


class AuthSettings(BaseSettings):
    """Email/password auth for the API (backend/apps/api/auth.py)."""

    model_config = _settings_config("AUTH_")

    dev_bypass: bool = Field(
        default=False,
        description=(
            "Local-dev-only escape hatch, independent of ENVIRONMENT. When true, every "
            "request is treated as one seeded test account (dev_bypass_email) and the "
            "frontend skips the sign-in screen entirely — no token required. Never enable "
            "this in a deployment reachable by more than one person."
        ),
    )
    signup_enabled: bool = Field(default=True, description="false = no self sign-up; create accounts with python -m backend.scripts.create_user.")
    invite_code: str = Field(default="", description="When set, sign-up requires this code. Set it before sharing a deployment.")
    token_ttl_days: int = Field(default=30, description="Sign-in lifetime; renewed while the user keeps using the app.")
    login_max_failures: int = Field(default=10, description="Failed sign-ins per IP or email per 15 minutes before 429.")
    dev_bypass_email: str = "dev@local.test"
    dev_bypass_password: str = "dev-password"
    """Only used to seed the bypass account at startup; never required to authenticate while dev_bypass is on."""


class GoogleDocAISettings(BaseSettings):
    """Google Document AI parser backend (backend/mcp_servers/indexing/parsers)."""

    model_config = _settings_config("GOOGLE_DOCAI_")

    use_parser: bool = Field(default=False, description="true = Google Document AI (cloud OCR), false = local Docling parser.")
    project_id: str = "project-db0446e6-5527-4b1b-ba4"
    location: str = "eu"
    processor_id: str = "45e6c24dbbd65e16"
    credentials_path: str | None = Field(
        default=None,
        description="Path to a service-account/ADC JSON key file. If set and GOOGLE_APPLICATION_CREDENTIALS isn't already exported, it's applied at startup.",
    )

    _normalize = field_validator("credentials_path", mode="before")(_blank_to_none)


class Settings:
    """Composed, single-import settings object. Use `from backend.config import settings`."""

    def __init__(self) -> None:
        self.environment: Environment = _RootSettings().environment

        self.ollama = OllamaSettings()
        self.openai = OpenAISettings()

        self.chat = ChatModelSettings()
        self.embedding = EmbeddingModelSettings()
        self.retrieval = RetrievalModelSettings()
        self.metadata = MetadataModelSettings()

        self.api = ApiSettings()
        self.db_mcp = DbMcpSettings()
        self.code_mcp = CodeMcpSettings()
        self.indexing_server = IndexingServerSettings()
        self.retrieval_server = RetrievalServerSettings()
        self.database = DatabaseSettings()
        self.document_store = DocumentStoreSettings()
        self.artifact_store = ArtifactStoreSettings()
        self.reranking = RerankingSettings()
        self.rag_v2 = LangchainRagSettings()
        self.google_docai = GoogleDocAISettings()
        self.auth = AuthSettings()
        self.agents = AgentsSettings()

    # --- environment helpers -------------------------------------------------
    @property
    def is_prod(self) -> bool:
        return self.environment is Environment.PROD

    @property
    def is_dev(self) -> bool:
        return self.environment is Environment.DEV

    # --- per-functionality active model (DEV/PROD-resolved) ------------------
    @property
    def chat_model(self) -> str:
        return self.chat.prod if self.is_prod else self.chat.dev

    @property
    def embedding_model(self) -> str:
        return self.embedding.prod if self.is_prod else self.embedding.dev

    @property
    def retrieval_model(self) -> str:
        return self.retrieval.prod if self.is_prod else self.retrieval.dev

    @property
    def retrieval_temperature(self) -> float:
        return self.retrieval.temperature


    @property
    def metadata_model(self) -> str:
        return self.metadata.prod if self.is_prod else self.metadata.dev

    @property
    def metadata_temperature(self) -> float:
        return self.metadata.temperature


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def get_api_settings() -> ApiSettings:
    """Back-compat accessor for code that only needs the API group."""
    return settings.api


def get_db_mcp_settings() -> DbMcpSettings:
    """Back-compat accessor for code that only needs the DB MCP group."""
    return settings.db_mcp


def get_code_mcp_settings() -> CodeMcpSettings:
    """Back-compat accessor for code that only needs the Code MCP group."""
    return settings.code_mcp
