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

    _normalize = field_validator("api_key", "base_url", mode="before")(_blank_to_none)


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


class RetrievalAgentModelSettings(BaseSettings):
    """Evaluation/answer LLM used by the standalone A2A retrieval agent (backend/apps/retrieval_agent)."""

    model_config = _settings_config("RETRIEVAL_AGENT_MODEL_")

    dev: str = "gpt-oss:20b"
    prod: str = "gpt-4o-mini"


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

    max_tool_result_chars: int = 8000
    """Hard cap on a single tool result's length before it enters conversation
    memory (and therefore gets resent to the LLM on every following turn).
    Tool authors are told to return a URL for large payloads (files, images)
    rather than inlining them, but this is a backstop for when a tool — or a
    model that ignores that instruction, e.g. base64-encoding an image into
    stdout instead of saving it — doesn't cooperate. Truncated, not the raw
    tool output shown to the user in the UI, which is unaffected."""

    log_level: str = "INFO"

    approval_required_tools: list[str] = Field(
        default_factory=list,
        description=(
            "Tool names that always pause for human approval before executing. "
            "Use ['*'] to require approval for every tool call by default. "
            "Sessions may override this at creation time."
        ),
    )

    interrupt_heartbeat_seconds: float = 15.0
    """How often to send an SSE keep-alive comment while waiting on a human decision."""

    interrupt_timeout_seconds: float | None = None
    """Optional hard timeout for a pending human-in-the-loop decision. None = wait forever."""

    session_idle_timeout_seconds: float = 3600.0
    """Sessions untouched for this long are evicted by the periodic cleanup task."""

    mongo_url: str = "mongodb://localhost:27017"
    """MongoDB connection string used to durably store conversations/messages."""

    mongo_db_name: str = "agentic_chat"
    """MongoDB database name for conversation persistence."""


class DbMcpSettings(BaseSettings):
    """Runtime configuration for the Database MCP server (backend/mcp_servers/db)."""

    model_config = _settings_config("DB_MCP_")

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


class RetrievalAgentSettings(BaseSettings):
    """Process/networking config for the standalone A2A retrieval agent."""

    model_config = _settings_config("RETRIEVAL_AGENT_")

    mcp_url: str = "http://localhost:8001/mcp"
    top_k: int = 10
    max_iterations: int = 5
    host: str = "localhost"
    port: int = 9999


class VectorStoreSettings(BaseSettings):
    """Chroma is run as its own server process (see start_mcp_servers.ps1)
    rather than opened in-process via PersistentClient, because the indexing
    and retrieval MCP servers are two separate long-lived processes — two
    independent PersistentClient instances against the same on-disk store
    don't reliably see each other's writes (Chroma's embedded client caches
    collection state per-process), so a document indexed after the retrieval
    server started could look like it doesn't exist. Routing both through one
    HttpClient/server gives them a single consistent view."""

    model_config = _settings_config("VECTOR_STORE_")

    dir: str = "data/vector_store"
    host: str = "localhost"
    port: int = 8010


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
    (backend/mcp_servers/indexing, retrieval `..._v2` tools).

    This is a parallel pipeline to the llama_index-based v1 tools, not a
    replacement — it writes/reads its own Chroma collections (named
    `collection_prefix + document_id`) on the same Chroma server, so v1 and
    v2 never see each other's data.
    """

    model_config = _settings_config("RAG_V2_")

    collection_prefix: str = "docv2_"

    # --- Chunking (indexing) -------------------------------------------------
    chunk_size: int = Field(default=1000, gt=0, description="Target chunk size (characters) for RecursiveCharacterTextSplitter.")
    chunk_overlap: int = Field(default=150, ge=0, description="Overlap (characters) between adjacent chunks.")

    # --- Hybrid retrieval + reranking ----------------------------------------
    default_top_k: int = Field(default=5, gt=0, description="Chunks returned when the caller doesn't specify top_k.")
    max_top_k: int = Field(default=25, gt=0, description="Hard ceiling on top_k accepted from a caller.")
    fetch_k_multiplier: int = Field(default=4, gt=0, description="Each of the dense/sparse retrievers fetches top_k * this many candidates before ensembling/reranking.")
    max_fetch_k: int = Field(default=40, gt=0, description="Hard ceiling on the fetch_k computed above.")
    dense_weight: float = Field(default=0.5, ge=0, description="EnsembleRetriever weight for the vector (Chroma) retriever.")
    sparse_weight: float = Field(default=0.5, ge=0, description="EnsembleRetriever weight for the keyword (BM25) retriever.")
    use_reranker: bool = Field(default=True, description="Rerank the ensembled candidates with a cross-encoder before truncating to top_k.")
    rerank_model: str = Field(default="cross-encoder/ms-marco-MiniLM-L-12-v2", description="HuggingFace cross-encoder model id used by the reranker.")


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
        self.retrieval_agent_model_cfg = RetrievalAgentModelSettings()
        self.metadata = MetadataModelSettings()

        self.api = ApiSettings()
        self.db_mcp = DbMcpSettings()
        self.code_mcp = CodeMcpSettings()
        self.indexing_server = IndexingServerSettings()
        self.retrieval_server = RetrievalServerSettings()
        self.retrieval_agent = RetrievalAgentSettings()
        self.vector_store = VectorStoreSettings()
        self.document_store = DocumentStoreSettings()
        self.artifact_store = ArtifactStoreSettings()
        self.reranking = RerankingSettings()
        self.rag_v2 = LangchainRagSettings()
        self.google_docai = GoogleDocAISettings()
        self.auth = AuthSettings()

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
    def retrieval_agent_model(self) -> str:
        return self.retrieval_agent_model_cfg.prod if self.is_prod else self.retrieval_agent_model_cfg.dev

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
