"""Universal settings for Ollama models and URLs used across the backend.

Every module that talks to Ollama (chat LLMs, embeddings, metadata
extraction, the retrieval MCP server, the generic MCP client app, etc.)
should import `settings` from here instead of hard-coding model names or
URLs. Values are configurable via environment variables / a `.env` file
next to `backend/`, with the previous hard-coded values kept as defaults.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parent.parent


class OllamaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        env_prefix="OLLAMA_",
        extra="ignore",
    )

    base_url: str = Field(
        default="http://localhost:11434",
        description="Base URL of the Ollama server (no /v1 suffix; append it where an OpenAI-compatible client needs it).",
    )
    chat_model: str = Field(
        default="gpt-oss:20b",  # "qwen2.5:7b", #"llama3.2",# "gpt-oss:20b",
        description="Default chat/completion model tag.",
    )
    embedding_model: str = Field(
        default="qwen3-embedding:4b",
        description="Default embedding model tag.",
    )


@lru_cache(maxsize=1)
def get_settings() -> OllamaSettings:
    return OllamaSettings()


settings = get_settings()
