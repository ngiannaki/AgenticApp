"""Streaming, interruptible version of backend.apps.client.agent.MCPAgent.

Same tool-calling loop as the CLI agent, but structured as an async
generator of SSE-ready events instead of a single blocking call that
returns the final answer. Before executing a tool call that the session
flags as requiring approval, the generator emits an "interrupt" event and
suspends on a Future until a human resolves it via the resume endpoint —
that's the human-in-the-loop hook.

One AgentRuntime is created once at startup (backend/apps/api/main.py) and
shared read-only across all sessions: it holds the single MCP connection,
the discovered tool list, and the LLM client (Ollama or OpenAI, depending on
ENVIRONMENT — see backend/config). Per-conversation state (memory, pending
interrupts, approval policy) lives on ChatSession instead.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import re
import uuid
from types import SimpleNamespace
from typing import Any, AsyncIterator

import anyio
import openai
from fastmcp import Client
from fastmcp.exceptions import ToolError

from backend.apps.api import events
from backend.apps.api.artifacts import FileTokenStore, materialize_artifacts
from backend.apps.api import skills as skills_lib
from backend.apps.api.config import ApiSettings
from backend.apps.api.sessions import ChatSession
from backend.config import get_async_openai_client, get_chat_extra_body, settings as app_settings
from backend.core.citations import _collect_and_stamp_citations, _render_citations
from backend.core.storage.artifact_store import ArtifactStore

logger = logging.getLogger("agentic_api")

ASK_USER_TOOL_NAME = "ask_user"
GET_SKILL_TOOL_NAME = "get_skill"

# Which MCP tools the model may see is an allowlist (settings.api.model_tools),
# not a denylist: every MCP server also registers tools meant only for the
# API itself — indexing_upload_document* (reads a server-local file_path),
# indexing_upload_and_index_document_v2 / indexing_delete_document_v2 (the
# Documents page's upload/delete), retrieval_get_document_chunks_v2 (the
# chunk browser's raw dump) — and a denylist silently exposes the next one
# somebody adds. FastAPI routes still call those through runtime.mcp /
# call_tool_isolated directly; only the model's view is filtered.

# Tool-call argument names that carry document ids. Any model-initiated call
# passing one is checked against the document registry before dispatch (see
# AgentRuntime._check_document_access) — a single guard covering retrieval,
# the code server's spreadsheet tools, and any future document-taking tool,
# instead of trusting every MCP server to implement its own ownership check.
_DOCUMENT_ID_ARGS = ("document_id", "document_ids")

# Tool parameters the API always fills in itself and hides from the model's
# view of the schema (see AgentRuntime.register_tool_schemas):
# - user_id: the signed-in user, so a model can't act as someone else;
# - file_token: a single-use grant letting the code sandbox download the one
#   document named in the call (backend/apps/api/artifacts.py).
_INJECTED_PARAMS = ("user_id", "file_token")

# Tool calls whose user_id argument must come from the authenticated session,
# never from the model. The model is told which document_ids are attached
# (see _attached_documents_note) but is never told or asked for a user_id —
# _handle_tool_call overwrites/injects this field itself right before
# dispatch, so a model can't retrieve or index another user's documents no
# matter what it puts in its own tool-call arguments.
#
# Previously a hardcoded set of tool names, which silently stopped scoping a
# tool the moment its name changed (e.g. a "_v2" tool) or a new user_id-taking
# tool was added and someone forgot to list it here. Now computed from each
# tool's own MCP-advertised input schema instead — see
# AgentRuntime.user_scoped_tool_names — so this is automatic.

# A local tool, not backed by any MCP server: handled entirely inside
# _handle_tool_call by pausing the turn on a PendingQuestion, the same way a
# human-approval interrupt pauses it. Modeled on AskUserQuestion-style
# clarifying questions — a prompt plus optional multiple-choice buttons, with
# free text always accepted as a fallback ("Other").
_ASK_USER_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ASK_USER_TOOL_NAME,
        "description": (
            "Ask the human a clarifying question and wait for their answer before "
            "continuing. Use this when you're blocked on a decision only the user can "
            "make — e.g. which of several ambiguous options they mean, or a choice "
            "with no safe default — rather than guessing. Optionally offer a short "
            "list of choices to show as buttons; the user can still type a free-text "
            "answer instead of picking one. Do not use this for information you could "
            "instead find with another tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The question to ask the user."},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short list of choices to offer as buttons (2-4 recommended). Omit for a free-text-only question.",
                },
            },
            "required": ["prompt"],
        },
    },
}


# One more local (non-MCP) tool, gated behind settings.api.skills_enabled —
# see backend/apps/api/skills.py. The skill *index* (name + description) is
# not a tool: it's injected straight into the session's system prompt by
# system_prompt_for() below, because a model cannot choose to look up a menu
# it doesn't know exists. get_skill fetches one skill's full body on demand,
# which is the only part that belongs behind a tool call.
_GET_SKILL_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": GET_SKILL_TOOL_NAME,
        "description": (
            "Read one skill's full step-by-step instructions by name. The available "
            "skills and what each one covers are listed in your system prompt. Call "
            "this BEFORE starting a task a listed skill covers, then follow what it "
            "says — it overrides your default approach for that task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill's exact name as listed in the system prompt."},
            },
            "required": ["name"],
        },
    },
}


_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _with_attached_documents_note(session: ChatSession, question: str) -> str:
    """Prepend a short note pointing the model at the right retrieval tools
    for any document_id it needs to work with this turn — both documents
    attached to this session (uploaded from the chat's own attach button)
    and bare document_ids the user typed directly (the expected flow for a
    document uploaded from the standalone Documents page, which isn't tied
    to any session). Only the composed text sent to the model is affected —
    the persisted/displayed chat history keeps the user's original
    `question` (see routers/chat.py, which passes `body.message` to
    persistence separately).

    Tool names here must match the MCP-aggregated names in self.tool_names
    (server key prefix + function name, e.g. "retrieval_retrieve_document_v2"
    for retrieval/server.py's retrieve_document_v2) — not the bare function
    names, which aren't valid tool calls and previously sent the model
    chasing "retrieve_document"/"get_document_info"/"read_document" (all
    404s from _handle_tool_call's unknown-tool check).

    Points at the "_v2" (LangChain hybrid retrieval + reranking) tools,
    matching upload_and_index in routers/files.py, which indexes into the
    same v2 collections — a document attached/uploaded through the API is
    only ever indexed with v2, so retrieval must use v2 too or it won't find
    it. Switch both together if you move back to v1.

    Also nudges the model toward get_table_of_contents_v2 first for
    structural questions, then retrieve_document_v2's `filters` param to
    scope the search — otherwise the model tends to skip straight to a blind
    top-k query, since this note is what it actually reads each turn (the
    full filters/TOC guidance lives in retrieve_document_v2's own docstring,
    but that's easy to miss buried in the tool list).

    A spreadsheet attached here (content_type csv/xlsx/xlsm) is a special
    case: routers/files.py's upload_file skips indexing for these entirely
    (see _SPREADSHEET_EXTENSIONS there), so no vector store exists for them
    and the retrieval guidance below does not apply. Those get a separate
    paragraph pointing at the code server's profile_spreadsheet /
    run_python_code_on_document instead — see spreadsheet-data-strategy
    skill for why (row-per-chunk retrieval can't guarantee completeness for
    tabular data the way a real dataframe query can). A bare document_id the
    user typed directly is always treated as indexed (Documents-page
    uploads always index, regardless of type), never as a raw spreadsheet.
    """
    attached_ids = {doc["document_id"] for doc in session.attached_documents}
    mentioned_ids = [m for m in _UUID_RE.findall(question) if m not in attached_ids]

    if not session.attached_documents and not mentioned_ids:
        return question

    _SPREADSHEET_CONTENT_TYPES = {"csv", "xlsx", "xlsm"}
    spreadsheet_docs = [d for d in session.attached_documents if d["content_type"] in _SPREADSHEET_CONTENT_TYPES]
    indexed_docs = [d for d in session.attached_documents if d["content_type"] not in _SPREADSHEET_CONTENT_TYPES]

    lines = []
    if indexed_docs:
        listing = ", ".join(
            f"{doc['filename']} (document_id={doc['document_id']}, type={doc['content_type']})"
            for doc in indexed_docs
        )
        lines.append(f"[Attached files (indexed): {listing}]")
    if spreadsheet_docs:
        listing = ", ".join(
            f"{doc['filename']} (document_id={doc['document_id']}, type={doc['content_type']})"
            for doc in spreadsheet_docs
        )
        lines.append(f"[Attached spreadsheets (NOT indexed): {listing}]")
    if mentioned_ids:
        lines.append(
            "[The message below also mentions this document_id, or these "
            f"document_ids, directly: {', '.join(mentioned_ids)} — treat each as "
            "a document already uploaded and indexed, e.g. via the Documents "
            "page, even though it has no filename here.]"
        )

    note_parts = ["\n".join(lines)]

    if indexed_docs or mentioned_ids:
        note_parts.append(
            "The indexed documents above were already uploaded and indexed into "
            "the vector store — there is no local file_path for them, so "
            "indexing_read_document (which needs a server-local file_path) will "
            "not work here. If the question asks about the document's structure, "
            "sections, or where something is located (e.g. \"what's in this "
            "document\", \"which section covers X\"), call "
            "retrieval_get_table_of_contents_v2(document_id) first to see the "
            "heading outline (or page/sheet coverage if no outline was captured) "
            "before searching — then use what you learn to scope the search, e.g. "
            "retrieval_retrieve_document_v2(document_id, query, filters={\"h1\": "
            "\"<heading>\"}) or filters={\"page\": N}, instead of a blind top-k "
            "query. If the user is specifically asking about tables (e.g. \"show "
            "me the tables\", \"what's in the pricing table\", \"only tabular "
            "data\"), add filters={\"chunk_content_type\": \"table\"} to restrict "
            "retrieval to table chunks only and exclude prose. For a plain content "
            "question, call retrieval_retrieve_document_v2(document_id, query) with "
            "a query describing what you're looking for (call it more than once "
            "with different queries if you need several kinds of information), or "
            "retrieval_get_document_info_v2(document_id) for metadata like "
            "section/page counts."
        )

    if spreadsheet_docs:
        note_parts.append(
            "The spreadsheets above were NOT indexed — there is no vector store "
            "for them, so retrieval_retrieve_document_v2/"
            "retrieval_get_table_of_contents_v2/retrieval_get_document_info_v2 will "
            "not find them; don't call those with these document_ids. Instead, call "
            "code_profile_spreadsheet(document_id) first to see sheet names, exact "
            "row/column counts, and column headers, then "
            "code_run_python_code_on_document(document_id, code) to actually answer "
            "the question with real pandas code against the full file — required "
            "for anything needing completeness or computation (\"sum this column\", "
            "\"every row where X\", \"how many rows match Y\"), which profiling "
            "alone can't answer."
        )

    note = "\n\n".join(note_parts)
    return f"{note}\n\n{question}"


def _extract_reasoning(message: Any) -> str | None:
    """Pull the model's chain-of-thought off a chat completion message, if present.

    Ollama's OpenAI-compatible endpoint adds a non-standard "reasoning" field
    to the message for thinking-capable models (e.g. gpt-oss:20b) when
    `reasoning_effort` is set (see get_chat_extra_body). It isn't part of
    the openai SDK's typed model, so it only surfaces via `model_extra`.
    """
    reasoning = getattr(message, "reasoning", None)
    if not reasoning:
        extra = getattr(message, "model_extra", None) or {}
        reasoning = extra.get("reasoning")
    return reasoning or None


def _extract_text(result: Any) -> str:
    """Mirror MCPAgent._execute_tool_call's result-unwrapping for fastmcp CallToolResult."""
    if isinstance(result, str):
        return result
    content = getattr(result, "content", result)
    if isinstance(content, list):
        text = "\n".join(b.text for b in content if hasattr(b, "text"))
    else:
        text = str(content) if content else str(result)
    return text or f"Tool returned no content. Raw result: {str(result)[:200]}"


def _looks_like_broken_connection(exc: Exception) -> bool:
    """True for failures that mean the underlying MCP connection itself died
    (vs. an ordinary application-level tool error like bad SQL or a denied
    permission). A dead stdio pipe surfaces server-side as an anyio
    ClosedResourceError/BrokenResourceError (observed 2026-10-01, after the
    host machine slept/resumed); fastmcp re-wraps that as a ToolError whose
    message is "Error calling tool '<name>': " + str(underlying_exc) -- and
    those anyio exceptions carry no message of their own, so the text
    trails off right after the colon. A real application-level error always
    has text there (e.g. a SQL error or a permissions.yaml denial), so this
    is what distinguishes the two without needing a stricter protocol-level
    signal.
    """
    if isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError)):
        return True
    if isinstance(exc, ToolError):
        text = str(exc)
        if text.startswith("Error calling tool"):
            return not text.rsplit(":", 1)[-1].strip()
    return False


class ResilientMcpClient:
    """Wraps the single process-wide stdio MCP connection (AgentRuntime.mcp)
    and transparently reconnects it when the connection itself has died,
    instead of requiring a manual `docker compose restart app` (see
    2026-10-01 incident: a ClosedResourceError left every subsequent call
    502ing until the container was restarted by hand).

    Every ChatSession under stdio transport shares this exact object (see
    AgentRuntime.acquire_session_client's docstring for why real per-session
    isolation isn't available there), so reconnecting here repairs every
    session at once rather than leaving already-handed-out references
    pointing at a dead client.

    Deliberately does NOT retry on an ordinary application-level tool error
    (bad SQL, a permissions.yaml denial, bad arguments) -- see
    _looks_like_broken_connection -- reconnecting there would just bolt a
    slow, pointless ~10s subprocess restart onto a failure that was never
    about the connection.
    """

    def __init__(self, servers: dict) -> None:
        self._servers = servers
        self._client: Client | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        client = Client(self._servers)
        await client.__aenter__()
        self._client = client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def _reconnect(self, failed_client: Client) -> None:
        async with self._lock:
            if self._client is not failed_client:
                # Another concurrent caller already reconnected us.
                return
            logger.warning("MCP connection appears broken, reconnecting...")
            with contextlib.suppress(Exception):
                await failed_client.__aexit__(None, None, None)
            await self.connect()
            logger.info("MCP connection re-established")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        client = self._client
        try:
            return await client.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 — inspected below, re-raised when not connection-shaped
            if not _looks_like_broken_connection(exc):
                raise
            await self._reconnect(client)
            return await self._client.call_tool(name, arguments)

    async def list_tools(self) -> Any:
        client = self._client
        try:
            return await client.list_tools()
        except Exception as exc:  # noqa: BLE001 — same reasoning as call_tool
            if not _looks_like_broken_connection(exc):
                raise
            await self._reconnect(client)
            return await self._client.list_tools()

    async def ping(self) -> bool:
        client = self._client
        try:
            return await client.ping()
        except Exception as exc:  # noqa: BLE001 — same reasoning as call_tool
            if not _looks_like_broken_connection(exc):
                raise
            await self._reconnect(client)
            return await self._client.ping()

    def new(self) -> Client:
        """Only meaningful under HTTP transport (isolated per-session
        clients, see AgentRuntime.acquire_session_client) -- delegates to
        the underlying raw client, unwrapped, same as before this class
        existed. Resilience here is scoped to the shared stdio client."""
        return self._client.new()


class AgentRuntime:
    """Shared, process-wide MCP connection + tool list + LLM client."""

    def __init__(
        self,
        mcp_client: ResilientMcpClient,
        llm_client: openai.AsyncOpenAI,
        settings: ApiSettings,
        isolated_sessions: bool,
    ) -> None:
        self.mcp = mcp_client
        self.llm = llm_client
        self.settings = settings
        self.tools: list[dict[str, Any]] = []
        self._skill_index: list[skills_lib.SkillMeta] = []
        self._injected_params: dict[str, set[str]] = {}
        self.file_tokens = FileTokenStore()
        self._artifact_store: ArtifactStore | None = None
        self.document_registry: Any = None
        """backend.apps.api.document_registry.DocumentRegistry, set by main.py's
        lifespan once Mongo is connected (None if it isn't). Used by
        _check_document_access to verify document ownership before any
        model-initiated tool call that names a document_id."""
        self._isolated_sessions = isolated_sessions
        """True when every chat session can safely get its own independent
        MCP protocol session (streamable-HTTP servers.json, run via
        start_mcp_servers.ps1 — each Client.new() opens a real new HTTP
        connection). False when every server is launched as a stdio
        subprocess of this process (servers.docker.json, the Docker image):
        there all sessions necessarily share the one already-initialized
        subprocess session underneath (fastmcp's StdioTransport.connect() is
        a no-op once connected, see transports/stdio.py), so handing out a
        "fresh" Client there would just re-send `initialize` over the
        shared pipe — confirmed in practice by the "Starting MCP server..."
        banners re-appearing in docker logs on every new chat, ~10s+ each,
        and able to race/break other sessions' in-flight requests while the
        server's init state flips back to Initializing. See
        AgentRuntime.acquire_session_client and sessions.py's ChatSession.
        """

    @classmethod
    async def connect(cls, servers: dict, settings: ApiSettings) -> "AgentRuntime":
        isolated_sessions = not any("command" in cfg for cfg in servers["mcpServers"].values())
        mcp_client = ResilientMcpClient(servers)
        await mcp_client.connect()
        llm_client = get_async_openai_client()
        runtime = cls(mcp_client, llm_client, settings, isolated_sessions=isolated_sessions)
        await runtime._load_tools()
        return runtime

    async def close(self) -> None:
        await self.mcp.close()

    async def acquire_session_client(self) -> tuple[Client | ResilientMcpClient, bool]:
        """A per-session MCP client, plus whether the caller owns its
        lifecycle (and must therefore close it when the session ends).

        Over HTTP (isolated_sessions=True) this is a fresh `Client.new()`,
        entered as its own independent connection/protocol session — so
        concurrent chat sessions never interleave requests on the same MCP
        session, and the caller owns it: it must `__aexit__` it when the
        chat session is deleted or evicted.

        Over stdio (isolated_sessions=False) real per-session isolation
        isn't available — every session necessarily shares the one
        subprocess session already opened in `connect()` — so this just
        hands back the shared `self.mcp` itself (a ResilientMcpClient, so a
        dead connection self-heals on next use — see its docstring), and the
        caller does NOT own it: closing it would tear down the one MCP
        connection for every other active session and the process as a
        whole. Only AgentRuntime.close() (app shutdown) may close it.
        """
        if not self._isolated_sessions:
            return self.mcp, False
        client = self.mcp.new()
        await client.__aenter__()
        return client, True

    async def call_tool_isolated(self, name: str, arguments: dict[str, Any]) -> Any:
        """One-shot tool call for request-scoped callers outside any
        ChatSession (e.g. the upload/delete endpoints in routers/files.py),
        which have no session object to hang a longer-lived client off of.
        Uses the same isolated-vs-shared logic as acquire_session_client so
        it's cheap and safe under both transports."""
        client, owns_lifecycle = await self.acquire_session_client()
        if not owns_lifecycle:
            return await client.call_tool(name, arguments)
        try:
            return await client.call_tool(name, arguments)
        finally:
            await client.__aexit__(None, None, None)

    def register_tool_schemas(self, specs: list[tuple[str, str, dict[str, Any] | None]]) -> None:
        """Set the model-visible MCP tools from (name, description, input
        schema) triples. Parameters the API fills in itself (_INJECTED_PARAMS:
        the caller's user_id, a sandbox file_token) are recorded per tool and
        removed from the schema the model sees — it never has to (and
        can't usefully) supply them."""
        self.tools = []
        self._injected_params = {}
        for name, description, schema in specs:
            schema = copy.deepcopy(schema or {"type": "object", "properties": {}})
            props = schema.get("properties") or {}
            injected = {p for p in _INJECTED_PARAMS if p in props}
            for p in injected:
                props.pop(p, None)
            if injected and isinstance(schema.get("required"), list):
                schema["required"] = [r for r in schema["required"] if r not in injected]
            self._injected_params[name] = injected
            self.tools.append(
                {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}
            )

    async def _load_tools(self) -> None:
        mcp_tools = await self.mcp.list_tools()
        allowed = set(self.settings.model_tools)
        expose_all = "*" in allowed
        hidden = sorted(t.name for t in mcp_tools if not expose_all and t.name not in allowed)
        if hidden:
            logger.info("Withheld from the model (not in API_MODEL_TOOLS): %s", ", ".join(hidden))
        self.register_tool_schemas(
            [(t.name, t.description or "", t.inputSchema) for t in mcp_tools if expose_all or t.name in allowed]
        )
        self.tools.append(_ASK_USER_TOOL_SCHEMA)

        if self.settings.skills_enabled:
            self._skill_index = skills_lib.load_skill_index(self.settings.skills_dir)
            if self._skill_index:
                self.tools.append(_GET_SKILL_TOOL_SCHEMA)
            logger.info(
                "Skills enabled: %d skill(s) found in %s: %s",
                len(self._skill_index),
                self.settings.skills_dir,
                ", ".join(s.name for s in self._skill_index) or "(none)",
            )

        logger.info("Connected to %d MCP tool(s) + local tools: %s", len(self.tools), ", ".join(self.tool_names))

    def system_prompt_for(self, base: str) -> str:
        """Append the skills index to a session's system prompt.

        The index is deliberately eager rather than behind a tool call. The
        first version of this feature exposed a `list_skills` tool instead,
        and the model never called it once — it cannot decide to consult a
        menu it doesn't know exists. The Agent Skills format expects exactly
        this split: name + description loaded up front for every skill, and
        only the (much larger) body fetched on demand, which is get_skill's
        whole job.

        Returns `base` unchanged when skills are disabled or none loaded, so
        a session's prompt is byte-for-byte what it was before this feature.

        The reconciling sentence below (about "only call a tool when
        needed") exists because `base` is normally DEFAULT_SYSTEM_PROMPT,
        whose very first rule is not to call a tool speculatively or just
        because it's available — from the model's side, get_skill looks
        exactly like that: a call that doesn't itself answer anything. Left
        unaddressed, that earlier, more foundational-sounding rule tends to
        suppress the very calls this block is asking for. Kept here rather
        than folded into DEFAULT_SYSTEM_PROMPT itself so the base prompt
        stays correct on its own when skills are disabled.
        """
        if not self._skill_index:
            return base
        catalog = "\n".join(f"- {s.name}: {s.description}" for s in self._skill_index)
        return (
            f"{base}\n\n"
            "AVAILABLE SKILLS\n"
            "Each skill below is a step-by-step procedure written for this system. "
            "Before you start a task that one of them covers, call get_skill with its "
            "exact name, read it, and follow it — a skill's instructions take priority "
            "over your own default approach for that task. This is required preparation, "
            "not a speculative or demonstrative tool call, so it is not covered by the "
            "'only call a tool when needed' rule above. Call get_skill at most once per "
            "skill per turn, and don't call it at all when no skill matches.\n\n"
            f"{catalog}"
        )

    @property
    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.tools]

    @property
    def user_scoped_tool_names(self) -> set[str]:
        """Tools whose MCP schema declares a `user_id` parameter — always
        filled in from the session (see _handle_tool_call), derived from
        each tool's own schema rather than a hardcoded list."""
        return {name for name, params in self._injected_params.items() if "user_id" in params}

    async def _chat(
        self, session: ChatSession, tools_enabled: bool = True
    ) -> AsyncIterator[tuple[str, Any]]:
        """Call the chat completions endpoint, recovering from a malformed-tool-call parse failure.

        Some models (observed with gpt-oss:20b) occasionally leak chain-of-thought
        text into the slot where a tool call is expected; Ollama's server tries to
        parse that text as JSON and returns a 500 ("error parsing tool call: raw=...
        err=invalid character ... looking for beginning of value"). That's a
        transient model hiccup, not a fatal error, so nudge the model once or
        twice and retry instead of ending the whole turn.

        Yields ("event", sse_dict) for status/error events to forward as-is, then
        exactly one of ("response", ChatCompletion) on success or ("failed", None)
        after an unrecoverable error (an "error" event has already been yielded).
        """
        max_recovery_attempts = 2
        for attempt in range(max_recovery_attempts + 1):
            try:
                response = await self.llm.chat.completions.create(
                    model=app_settings.chat_model,
                    messages=session.memory.messages(),
                    tools=(self.tools or None) if tools_enabled else None,
                    temperature=self.settings.temperature,
                    extra_body=get_chat_extra_body(),
                )
                yield ("response", response)
                return
            except openai.APIStatusError as exc:
                malformed_tool_call = "error parsing tool call" in str(exc)
                if malformed_tool_call and attempt < max_recovery_attempts:
                    logger.warning(
                        "Model produced a malformed tool call (attempt %d/%d): %s",
                        attempt + 1,
                        max_recovery_attempts,
                        exc,
                    )
                    yield ("event", events.status("model output could not be parsed as a tool call, retrying"))
                    session.memory.add_user(
                        "Your previous message could not be parsed: reply with either one valid tool "
                        "call, or plain final-answer text — never a mix of prose and partial JSON."
                    )
                    continue
                logger.exception("LLM chat call failed")
                yield ("event", events.error(f"LLM call failed: {exc}"))
                yield ("failed", None)
                return
            except Exception as exc:  # noqa: BLE001 — surface upstream failure and end the turn
                logger.exception("LLM chat call failed")
                yield ("event", events.error(f"LLM call failed: {exc}"))
                yield ("failed", None)
                return

    async def run_stream(self, session: ChatSession, question: str) -> AsyncIterator[dict[str, Any]]:
        """Drive one user turn for `session`, yielding SSE-ready events as it goes."""
        session.touch()
        session.memory.add_user(_with_attached_documents_note(session, question))
        max_iterations = self.settings.max_iterations
        citations: list[dict[str, Any]] = []
        chunk_numbers: dict[str, int] = {}

        for iteration in range(1, max_iterations + 1):
            session.touch()
            yield events.status(f"iteration {iteration}/{max_iterations}")

            response = None
            async for kind, payload in self._chat(session, tools_enabled=True):
                if kind == "event":
                    yield payload
                elif kind == "response":
                    response = payload
                elif kind == "failed":
                    return
            if response is None:
                return

            message = response.choices[0].message
            reasoning = _extract_reasoning(message)
            if reasoning:
                yield events.reasoning(reasoning)

            if not message.tool_calls:
                raw_answer = message.content or ""
                answer, cited = _render_citations(raw_answer, citations)
                session.memory.add_assistant(answer)
                if not raw_answer.strip() and iteration < max_iterations:
                    yield events.status("empty response from model, retrying")
                    continue
                if citations:
                    yield events.citations(cited)
                yield events.message(answer)
                yield events.done(answer, iteration)
                return

            session.memory.add_assistant(
                message.content,
                tool_calls=[tc.model_dump() for tc in message.tool_calls],
            )

            for call in message.tool_calls:
                async for event in self._handle_tool_call(session, call, citations, chunk_numbers):
                    yield event

        # Iteration budget exhausted: ask once more, tools disabled, to force an answer.
        yield events.status("max iterations reached, forcing final answer")
        response = None
        async for kind, payload in self._chat(session, tools_enabled=False):
            if kind == "event":
                yield payload
            elif kind == "response":
                response = payload
            elif kind == "failed":
                return
        if response is None:
            return
        final_message = response.choices[0].message
        reasoning = _extract_reasoning(final_message)
        if reasoning:
            yield events.reasoning(reasoning)
        raw_final = final_message.content or "Unable to produce a final answer in the allotted iterations."
        final, cited = _render_citations(raw_final, citations)

        session.memory.add_assistant(final)
        if citations:
            yield events.citations(cited)
        yield events.message(final)
        yield events.done(final, max_iterations)

    async def _handle_tool_call(
        self,
        session: ChatSession,
        call: Any,
        citations: list[dict[str, Any]],
        chunk_numbers: dict[str, int],
    ) -> AsyncIterator[dict[str, Any]]:
        name = call.function.name
        call_id = str(uuid.uuid4())
        try:
            arguments = json.loads(call.function.arguments) if call.function.arguments else {}
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as exc:
            # A malformed tool call is the model's mistake to correct, not a
            # reason to end the whole turn — hand it back as a failed result.
            text = f"Error calling tool '{name}': arguments were not a valid JSON object ({exc}). Retry with valid JSON."
            yield events.tool_call(call_id, name, {"_raw": call.function.arguments})
            yield events.tool_result(call_id, name, False, text)
            session.memory.add_tool_result(name, text, tool_call_id=call.id)
            return

        yield events.tool_call(call_id, name, arguments)

        if name == ASK_USER_TOOL_NAME:
            async for event in self._handle_ask_user(session, call, call_id, arguments):
                yield event
            return

        if name == GET_SKILL_TOOL_NAME:
            skill_name = str(arguments.get("name") or "").strip()
            body = skills_lib.read_skill_body(self.settings.skills_dir, skill_name) if skill_name else None
            if body is None:
                available = ", ".join(s.name for s in self._skill_index)
                text = f"Error calling tool 'get_skill': unknown skill {skill_name!r}. Available skills: {available}."
                yield events.tool_result(call_id, name, False, text)
            else:
                yield events.tool_result(call_id, name, True, body)
                text = body
            session.memory.add_tool_result(name, text, tool_call_id=call.id)
            return

        if name not in self.tool_names:
            available = ", ".join(self.tool_names)
            text = (
                f"Error calling tool '{name}': unknown tool. "
                f"Available tools are: {available}. Call one of these exactly, "
                "using the arguments from the conversation or a previous tool result, "
                "or answer directly without a tool call if none of them fit."
            )
            logger.warning("Model called unknown tool '%s' (available: %s)", name, available)
            yield events.tool_result(call_id, name, False, text)
            session.memory.add_tool_result(name, text, tool_call_id=call.id)
            return

        if session.requires_approval(name):
            pending = session.new_interrupt(call_id, name, arguments)
            yield events.interrupt(pending.interrupt_id, call_id, name, arguments)

            decision = await self._await_decision(pending.future)
            session.pending_interrupts.pop(pending.interrupt_id, None)

            if decision.action == "reject":
                text = f"Tool call '{name}' was rejected by the user."
                if decision.reason:
                    text += f" Reason: {decision.reason}"
                yield events.tool_result(call_id, name, False, text)
                session.memory.add_tool_result(name, text, tool_call_id=call.id)
                return
            if decision.action == "edit" and decision.arguments is not None:
                arguments = decision.arguments

        # After the approval block on purpose: an "edit" decision can replace
        # the arguments, document_id included, so check what will actually run.
        denied = await self._check_document_access(session, arguments)
        if denied is not None:
            text = f"Error calling tool '{name}': {denied}"
            logger.warning("[ACCESS] user=%s tool=%s denied: %s", session.user_id, name, denied)
            yield events.tool_result(call_id, name, False, text)
            session.memory.add_tool_result(name, text, tool_call_id=call.id)
            return

        injected = self._injected_params.get(name, set())
        if "user_id" in injected:
            arguments["user_id"] = session.user_id
        if "file_token" in injected:
            # Only ever for a document that just passed _check_document_access.
            document_id = arguments.get("document_id")
            arguments["file_token"] = self.file_tokens.issue(str(document_id)) if document_id else ""

        yield events.status(f"calling {name}")
        timeout = self.settings.tool_call_timeout_seconds
        try:
            result = await asyncio.wait_for(session.mcp.call_tool(name, arguments), timeout=timeout)
            text = _extract_text(result)
            ok = True
        except asyncio.TimeoutError:
            text = f"Error calling tool '{name}': timed out after {timeout:.0f}s."
            ok = False
            logger.error(text)
        except Exception as exc:  # noqa: BLE001 — surface any tool failure back to the model
            text = f"Error calling tool '{name}': {exc}"
            ok = False
            logger.error(text)

        if ok:
            text = self.materialize_artifacts(text)
        text = _collect_and_stamp_citations(citations, chunk_numbers, name, ok, text)
        yield events.tool_result(call_id, name, ok, text)
        session.memory.add_tool_result(name, text, tool_call_id=call.id)

    def materialize_artifacts(self, text: str) -> str:
        """Store any inline files in a tool result and replace them with URLs
        (see backend/apps/api/artifacts.py)."""
        if '"data_base64"' not in text:
            return text
        if self._artifact_store is None:
            self._artifact_store = ArtifactStore()
        return materialize_artifacts(text, self._artifact_store, app_settings.code_mcp.artifact_public_base_url)

    async def execute_tool(self, user_id: str, name: str, arguments: dict[str, Any], approved: bool = False) -> tuple[bool, str]:
        """Run one MCP tool for `user_id` outside a chat session — the agents
        service's path (POST /internal/tools/call). Same rules as a chat tool
        call: allowlisted tools only, mandatory approvals (the agent must have
        asked the user and passes approved=True), document ownership,
        injected user_id/file_token, timeout, artifact materialization."""
        if name not in self._injected_params:
            return False, f"Error calling tool '{name}': unknown or unavailable tool."
        if name in self.settings.mandatory_approval_tools and not approved:
            return False, f"Error calling tool '{name}': this action needs the user's approval first."
        arguments = dict(arguments)
        denied = await self._check_document_access(SimpleNamespace(user_id=user_id), arguments)
        if denied is not None:
            return False, f"Error calling tool '{name}': {denied}"
        injected = self._injected_params[name]
        if "user_id" in injected:
            arguments["user_id"] = user_id
        if "file_token" in injected:
            document_id = arguments.get("document_id")
            arguments["file_token"] = self.file_tokens.issue(str(document_id)) if document_id else ""
        timeout = self.settings.tool_call_timeout_seconds
        try:
            result = await asyncio.wait_for(self.mcp.call_tool(name, arguments), timeout=timeout)
        except asyncio.TimeoutError:
            return False, f"Error calling tool '{name}': timed out after {timeout:.0f}s."
        except Exception as exc:  # noqa: BLE001 - reported back to the agent
            return False, f"Error calling tool '{name}': {exc}"
        return True, self.materialize_artifacts(_extract_text(result))

    async def _check_document_access(self, session: ChatSession, arguments: dict[str, Any]) -> str | None:
        """Return a denial reason if `arguments` name any document the
        session's user doesn't own, else None.

        Checked here, centrally, rather than trusting each MCP server: the
        code server's spreadsheet tools take a bare document_id and have no
        notion of users at all, and retrieval's own collection-metadata check
        is a second line of defence, not the only one. Unknown ids get the
        same message as someone else's so the model (or a prompt-injected
        instruction) can't probe which ids exist.
        """
        doc_ids: list[str] = []
        for key in _DOCUMENT_ID_ARGS:
            value = arguments.get(key)
            if isinstance(value, list):
                doc_ids.extend(str(v) for v in value if v)
            elif value:
                # Anything else non-empty (normally a str, but don't let an
                # int/dict slip past unchecked just because of its type).
                doc_ids.append(str(value))
        if not doc_ids:
            return None

        registry = self.document_registry
        if registry is None:
            if app_settings.auth.dev_bypass:
                return None  # single-user local dev without Mongo: nothing to check against
            return "the document registry is unavailable, so document access can't be verified."

        for doc_id in doc_ids:
            if not await registry.is_owner(doc_id, session.user_id):
                return f"document_id={doc_id!r} was not found among this user's documents."
        return None

    async def _handle_ask_user(
        self, session: ChatSession, call: Any, call_id: str, arguments: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Pause the turn and ask the human a question, the ask_user tool's whole
        job — never dispatched to MCP, since the answer has to come from whoever
        is looking at the frontend, not from a tool server."""
        prompt = str(arguments.get("prompt") or "").strip()
        options = [str(o) for o in (arguments.get("options") or []) if str(o).strip()]

        if not prompt:
            text = "Error calling tool 'ask_user': 'prompt' is required."
            yield events.tool_result(call_id, ASK_USER_TOOL_NAME, False, text)
            session.memory.add_tool_result(ASK_USER_TOOL_NAME, text, tool_call_id=call.id)
            return

        pending = session.new_question(call_id, prompt, options, allow_other=True)
        yield events.question(pending.question_id, call_id, prompt, options, allow_other=True)

        answer = await self._await_answer(pending.future)
        session.pending_questions.pop(pending.question_id, None)

        yield events.tool_result(call_id, ASK_USER_TOOL_NAME, True, answer.answer)
        session.memory.add_tool_result(ASK_USER_TOOL_NAME, answer.answer, tool_call_id=call.id)

    async def _await_answer(self, future: "asyncio.Future") -> Any:
        """Same wait/timeout shape as _await_decision, for a pending question."""
        timeout = self.settings.interrupt_timeout_seconds
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            from backend.apps.api.schemas import QuestionAnswerRequest

            return QuestionAnswerRequest(answer="(no response — timed out waiting for the user)")

    async def _await_decision(self, future: "asyncio.Future") -> Any:
        """Wait on a pending interrupt.

        `EventSourceResponse`'s built-in ping loop (routers/chat.py) keeps the
        connection alive independently of this coroutine, so all that's needed
        here is to respect the configured decision timeout, if any.
        """
        timeout = self.settings.interrupt_timeout_seconds
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            from backend.apps.api.schemas import InterruptDecisionRequest

            return InterruptDecisionRequest(action="reject", reason="Timed out waiting for human decision.")
