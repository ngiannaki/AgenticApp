# LLMS
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
#from config.settings import get_use_google_api

from backend.config import settings

logger = logging.getLogger(__name__)
PROJECT_ID = settings.google_docai.project_id
LOCATION = "us-central1"
OLLAMA_BASE_URL = settings.ollama.base_url
OLLAMA_CHAT_MODEL = settings.chat_model
OLLAMA_EMBEDDING_MODEL = settings.embedding_model
# VERTEX_CHAT_MODEL = os.getenv("VERTEX_CHAT_MODEL", "gemini-2.5-flash")
# VERTEX_EMBEDDING_MODEL = os.getenv("VERTEX_EMBEDDING_MODEL", "text-embedding-005")
# VERTEX_MAX_OUTPUT_TOKENS = int(os.getenv("VERTEX_MAX_OUTPUT_TOKENS", "8192"))


# # If the env var is not already set, use the local ADC file in this repo.
# _DEFAULT_ADC = Path(__file__).resolve().parents[2] / "google" / "application_default_credentials.json"
# if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and _DEFAULT_ADC.exists():
# 	os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_DEFAULT_ADC)

# os.environ.setdefault("GOOGLE_CLOUD_PROJECT", PROJECT_ID)

# llm_llama = ChatOllama(model="llama3.2", temperature=0, max_tokens=1000, base_url=OLLAMA_BASE_URL)
# llm_coder_qwen = ChatOllama(model="qwen2.5-coder:7b", temperature=0, max_tokens=1000, base_url=OLLAMA_BASE_URL)
# llm_phi = ChatOllama(model=OLLAMA_CHAT_MODEL, temperature=0, max_tokens=1000, base_url=OLLAMA_BASE_URL)
# llm_coder_deepseek = ChatOllama(model="deepseek-coder:6.7b", temperature=0, max_tokens=1000, base_url=OLLAMA_BASE_URL)


