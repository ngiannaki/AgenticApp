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


def get_ollama_extra_body() -> dict[str, Any]:
    """`extra_body` to pass into every `chat.completions.create()` call so Ollama
    honors `settings.ollama.num_ctx` instead of silently truncating to its own
    2048-token default, and requests a reasoning effort from thinking-capable
    models (e.g. gpt-oss:20b) so their chain-of-thought comes back in the
    response message's "reasoning" field. No-op in PROD, where the client
    talks to real OpenAI.
    """
    if settings.is_prod:
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
