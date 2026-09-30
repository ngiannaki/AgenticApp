"""One place that turns `settings` into actual LLM clients.

Every direct LLM call in the backend — the chat agent, the retrieval agent —
goes through `openai.AsyncOpenAI`, never a provider-specific SDK: in DEV that
client points at Ollama's `/v1` endpoint, in PROD it points at real OpenAI.
Call sites only ever pick a model name (`settings.chat_model`, ...); they
never branch on `settings.environment` themselves.

`get_llamaindex_llm`/`get_llamaindex_embedding` are the one exception: the
RAG pipeline (indexing, retrieval, metadata extraction) is built on
llama_index, whose `OpenAI`/`OpenAIEmbedding` wrapper classes validate the
model name against a fixed list of real OpenAI models and hard-fail on an
Ollama tag like "gpt-oss:20b" — so DEV still needs llama_index's native
Ollama classes there. That branch lives only here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI

from .settings import settings


def _client_kwargs() -> dict[str, Any]:
    if settings.is_prod:
        return {"api_key": settings.openai.api_key, "base_url": settings.openai.base_url}
    return {"api_key": "ollama", "base_url": f"{settings.ollama.base_url}/v1"}


@lru_cache(maxsize=1)
def get_async_openai_client() -> AsyncOpenAI:
    """The one AsyncOpenAI client shared by every functionality."""
    return AsyncOpenAI(**_client_kwargs())


def get_chat_extra_body() -> dict[str, Any]:
    """`extra_body` to pass into every `chat.completions.create()` call.

    DEV (Ollama): requests `num_ctx` (Ollama's own default is a mere 2048
    tokens regardless of what the model supports) and, for a thinking-capable
    model like gpt-oss:20b, a reasoning effort so its chain-of-thought comes
    back in the response message's "reasoning" field.

    PROD (real OpenAI): empty unless `settings.openai.reasoning_effort` is
    set. Only set that when CHAT_MODEL_PROD (or whichever _PROD model this
    call is using) is itself a reasoning-capable model — OpenAI's
    /v1/chat/completions rejects `reasoning_effort` outright for a classic
    model like gpt-4o-mini, and separately rejects tool calls for a reasoning
    model with *no* `reasoning_effort` at all, so the two must be kept in
    sync with the model actually configured.
    """
    if settings.is_prod:
        if settings.openai.reasoning_effort:
            return {"reasoning_effort": settings.openai.reasoning_effort}
        return {}
    body: dict[str, Any] = {"options": {"num_ctx": settings.ollama.num_ctx}}
    if settings.ollama.reasoning_effort:
        body["reasoning_effort"] = settings.ollama.reasoning_effort
    return body


def get_llamaindex_llm(model: str, temperature: float = 0.0, **kwargs: Any):
    """A llama_index LLM for the given model, using Ollama in DEV and OpenAI in PROD."""
    if settings.is_prod:
        from llama_index.llms.openai import OpenAI as LlamaOpenAI

        return LlamaOpenAI(model=model, temperature=temperature, api_key=settings.openai.api_key, api_base=settings.openai.base_url, **kwargs)

    from llama_index.llms.ollama import Ollama as LlamaOllama

    kwargs.setdefault("context_window", settings.ollama.num_ctx)
    return LlamaOllama(model=model, base_url=settings.ollama.base_url, temperature=temperature, **kwargs)


def get_llamaindex_embedding(model: str, **kwargs: Any):
    """A llama_index embedding model for the given model, using Ollama in DEV and OpenAI in PROD."""
    if settings.is_prod:
        from llama_index.embeddings.openai import OpenAIEmbedding as LlamaOpenAIEmbedding

        return LlamaOpenAIEmbedding(model=model, api_key=settings.openai.api_key, api_base=settings.openai.base_url, **kwargs)

    from llama_index.embeddings.ollama import OllamaEmbedding as LlamaOllamaEmbedding

    return LlamaOllamaEmbedding(model_name=model, base_url=settings.ollama.base_url, **kwargs)


def get_langchain_llm(model: str, temperature: float = 0.0, **kwargs: Any):
    """A LangChain chat model for the given model, using Ollama in DEV and OpenAI in PROD.

    Parallel to get_llamaindex_llm above, for the LangChain-based v2 RAG
    pipeline (backend/mcp_servers/indexing, retrieval `..._v2` tools). Kept
    separate from the llama_index helpers rather than shared, since the two
    RAG pipelines use unrelated client libraries.
    """
    if settings.is_prod:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, temperature=temperature, api_key=settings.openai.api_key, base_url=settings.openai.base_url, **kwargs)

    from langchain_ollama import ChatOllama

    kwargs.setdefault("num_ctx", settings.ollama.num_ctx)
    return ChatOllama(model=model, base_url=settings.ollama.base_url, temperature=temperature, **kwargs)


def get_langchain_embedding(model: str, **kwargs: Any):
    """A LangChain embedding model for the given model, using Ollama in DEV and OpenAI in PROD.

    Parallel to get_llamaindex_embedding above, for the LangChain-based v2
    RAG pipeline. Must embed into the same vector space as the llama_index
    pipeline's model (same `settings.embedding_model`) so DEV/PROD stay
    consistent, but v2 writes to its own Chroma collections
    (`settings.rag_v2.collection_prefix`) rather than sharing v1's.
    """
    if settings.is_prod:
        from langchain_openai import OpenAIEmbeddings

        # OpenAIEmbeddings defaults chunk_size=1000 (texts per request, not a
        # token cap) — for large documents split into many chunks, 1000 of
        # them can sum past OpenAI's 300k-tokens-per-request limit in one
        # batch (observed indexing a ~650k-token document), so use a smaller
        # batch size instead of the client-side per-request limit.
        kwargs.setdefault("chunk_size", 100)
        return OpenAIEmbeddings(model=model, api_key=settings.openai.api_key, base_url=settings.openai.base_url, **kwargs)

    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(model=model, base_url=settings.ollama.base_url, **kwargs)
