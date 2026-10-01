-- One Postgres for the whole app (replaces MongoDB + Chroma).
--
-- Applied by backend/core/db/migrate.py (at API / indexing / retrieval
-- startup, serialized with an advisory lock). Each file runs once, in name
-- order, inside its own transaction; add 0002_..., 0003_... for later
-- changes instead of editing this one.
--
-- Schemas:
--   app  - users, auth tokens, conversations/messages, document registry
--   rag  - indexed documents, their chunks, embeddings and full-text vectors
--   ws_* - per-user DB-tool workspaces; created on demand by the DB MCP
--          server under its own restricted role, never here

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS app;
CREATE SCHEMA IF NOT EXISTS rag;

-- --- app ------------------------------------------------------------------

CREATE TABLE app.users (
    id            text PRIMARY KEY,
    email         text NOT NULL UNIQUE,          -- stored lowercased
    password_hash text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Only a SHA-256 of each bearer token is stored, so a database leak doesn't
-- hand out live sessions.
CREATE TABLE app.auth_tokens (
    token_hash   text PRIMARY KEY,
    user_id      text NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,
    last_used_at timestamptz
);
CREATE INDEX auth_tokens_user_idx ON app.auth_tokens (user_id);

CREATE TABLE app.conversations (
    id         text PRIMARY KEY,
    user_id    text NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    title      text NOT NULL DEFAULT 'New chat',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_user_updated_idx ON app.conversations (user_id, updated_at DESC);

CREATE TABLE app.messages (
    seq             bigserial PRIMARY KEY,       -- stable ordering within a conversation
    id              text NOT NULL UNIQUE,
    conversation_id text NOT NULL REFERENCES app.conversations(id) ON DELETE CASCADE,
    role            text NOT NULL,
    content         text NOT NULL DEFAULT '',
    steps           jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_conversation_idx ON app.messages (conversation_id, seq);

-- Per-user document registry: who owns which uploaded file, and whether it's
-- been indexed. The source of truth for ownership checks in the API.
CREATE TABLE app.documents (
    id             text PRIMARY KEY,
    user_id        text NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    original_name  text,
    stored_name    text,
    file_type      text,
    status         text NOT NULL,                -- stored | indexing | indexed | failed
    chunks_indexed integer NOT NULL DEFAULT 0,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX documents_user_created_idx ON app.documents (user_id, created_at DESC);

-- --- rag ------------------------------------------------------------------

CREATE TABLE rag.documents (
    document_id     text PRIMARY KEY,
    user_id         text NOT NULL,
    document_name   text,
    content_type    text,
    embedding_model text NOT NULL,               -- query embeddings must use the same model
    toc             jsonb,                       -- heading outline captured at index time
    chunk_count     integer NOT NULL DEFAULT 0,
    indexed_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rag_documents_user_idx ON rag.documents (user_id);

-- `embedding` has no fixed dimension so DEV (Ollama) and PROD (OpenAI)
-- embedding models can coexist; searches are always scoped to a handful of
-- documents (exact cosine scan over their chunks), so no ANN index is needed
-- at this size. 'simple' text search config = no stemming/stop-words, which
-- behaves the same for any language.
CREATE TABLE rag.chunks (
    chunk_id    text PRIMARY KEY,
    document_id text NOT NULL REFERENCES rag.documents(document_id) ON DELETE CASCADE,
    chunk_index integer NOT NULL,
    content     text NOT NULL,
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding   vector NOT NULL,
    tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
);
CREATE INDEX rag_chunks_document_idx ON rag.chunks (document_id, chunk_index);
CREATE INDEX rag_chunks_tsv_idx ON rag.chunks USING gin (tsv);
CREATE INDEX rag_chunks_metadata_idx ON rag.chunks USING gin (metadata jsonb_path_ops);
