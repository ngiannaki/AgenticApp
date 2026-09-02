# Enhanced Metadata Features

## Overview

The enhanced metadata system uses LlamaIndex's built-in functionality to extract rich metadata from documents:

- **Hierarchical Chunking** - Preserves semantic relationships between chunks
- **Document Summaries** - LLM-generated concise summaries using Ollama
- **Section Extraction** - Automatically identifies main topics/sections
- **Document Statistics** - Word count, character count, structure info
- **Hierarchical Relationships** - Parent-child chunk relationships for better context

---

## Components

### 1. `MetadataEnhancer` (metadata_enhancer.py)

#### Methods:

**`extract_sections(text, top_k=5)`**
- Extracts main topics/sections from document
- Uses LLM to identify important areas
- Returns list of sections with titles and descriptions
- Example:
  ```python
  sections = enhancer.extract_sections("document text")
  # Returns: [
  #   {"title": "Introduction", "description": "..."},
  #   {"title": "Methods", "description": "..."},
  # ]
  ```

**`generate_summary(text, max_length=300)`**
- Creates LLM-based document summary
- Uses Ollama for generation
- Respects character limit
- Example:
  ```python
  summary = enhancer.generate_summary("document text")
  # Returns: "This document discusses..."
  ```

**`extract_metadata(text, document_name)`**
- Comprehensive metadata extraction
- Returns dict with:
  - `document_name` - Original filename
  - `word_count` - Total words
  - `char_count` - Total characters
  - `document_type` - Type (pdf, doc, etc)
  - `summary` - Generated summary
  - `sections` - Extracted sections with hierarchy

### 2. `HierarchicalChunker` (metadata_enhancer.py)

Uses LlamaIndex's `HierarchicalNodeParser` for smart chunking.

#### Features:

- **Multi-level hierarchy** - Creates chunks at different granularity levels
  - Large chunks: 2048 tokens (broad context)
  - Medium chunks: 512 tokens (focused context)  
  - Small chunks: 128 tokens (precise details)

- **Semantic relationships** - Chunks know their parent/child relationships
- **Chunk overlap** - 20 tokens overlap between chunks for context continuity

#### Methods:

**`create_hierarchical_chunks(text, document_id, document_name, page_number)`**
- Creates hierarchical chunks from text
- Each chunk has metadata:
  ```python
  {
    "text": "chunk text",
    "metadata": {
      "document_id": "uuid",
      "document_name": "profile.pdf",
      "page_number": 1,
      "chunk_index": 0,
      "chunk_id": "unique-chunk-id",
      "hierarchy_level": 0,  # 0=large, 1=medium, 2=small
      "parent_chunk_id": "parent-id",  # links to parent
      "source": "hierarchical_parse"
    }
  }
  ```

**`create_chunks_from_pages(pages, document_id, document_name)`**
- Processes multiple pages
- Maintains page boundaries in metadata
- Creates hierarchical structure across pages

### 3. Enhanced VectorStore Methods (vector_store.py)

**`index_document_enhanced(document_id, parsed_content, document_name, use_hierarchical, extract_metadata)`**

Parameters:
- `document_id` - Document UUID
- `parsed_content` - Dict with 'pages' and 'full_text' from parser
- `document_name` - Original filename for metadata
- `use_hierarchical` - Enable hierarchical chunking (default: True)
- `extract_metadata` - Extract summary/sections (default: True)

Returns:
```python
{
  "success": True,
  "document_id": "uuid",
  "chunks_indexed": 15,
  "collection_name": "doc_uuid",
  "persist_path": "data/vector_store/doc_uuid",
  "enhanced_metadata": {
    "document_name": "profile.pdf",
    "word_count": 2543,
    "char_count": 14220,
    "document_type": "pdf",
    "summary": "This document details...",
    "sections": [
      {"title": "Background", "description": "..."},
      {"title": "Experience", "description": "..."},
    ],
    "section_count": 5
  }
}
```

---

## New MCP Tool

### `parse_and_index_enhanced`

**Available in:** Indexing MCP (port 8000)

**Parameters:**
- `document_id` (required) - Document UUID from upload
- `document_name` (optional) - Original filename
- `use_hierarchical` (optional) - Enable hierarchical chunking (default: True)

**Example Usage:**
```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "parse_and_index_enhanced",
      "arguments": {
        "document_id": "e812da62-6d05-407d-9b14-0c1efd26bad3",
        "document_name": "Profile.pdf",
        "use_hierarchical": true
      }
    }
  }'
```

---

## Storage Structure

```
data/vector_store/doc_{uuid}/
├── chroma.db (vector database)
├── metadata.json (enhanced document metadata)
└── sections.json (extracted sections)

Each chunk metadata includes:
- hierarchy_level (0, 1, or 2)
- parent_chunk_id (for traversal)
- section_info (which section it belongs to)
- document statistics
```

---

## Benefits for Retrieval

### 1. Better Context in Responses
- Hierarchical chunks provide multi-level context
- Parent chunks offer broader context
- Child chunks provide precise details

### 2. Improved Relevance
- Metadata helps filter results by section
- Summary helps evaluate document relevance
- Statistics inform retrieval strategy

### 3. Enhanced Citations
- Chunk hierarchy shows context
- Section info provides document organization context
- Parent-child relationships show full context

### 4. Example Query Response
```python
{
  "success": True,
  "answer": "The person has 10 years of experience...",
  "source_chunks": [
    {
      "text": "chunk text",
      "score": 0.92,
      "metadata": {
        "document_name": "Profile.pdf",
        "section": "Experience",
        "hierarchy_level": 2,
        "parent_chunk_id": "parent-123"
      }
    }
  ],
  "document_summary": "This is a professional profile..."
}
```

---

## Configuration

Edit in `vector_store.py` and `metadata_enhancer.py`:

```python
# Chunk sizes for hierarchical parsing
chunk_sizes = [2048, 512, 128]  # Large, medium, small

# LLM for metadata extraction
OLLAMA_CHAT_MODEL = "phi4"  # or other model

# Summary length
max_summary_length = 300  # characters
```

---

## Example Usage

### Step 1: Upload PDF
```python
# Returns: document_id
```

### Step 2: Enhanced Indexing
```python
result = await session.call_tool(
    "parse_and_index_enhanced",
    arguments={
        "document_id": document_id,
        "document_name": "Profile.pdf",
        "use_hierarchical": True,
    }
)

# Get enhanced metadata
summary = result["enhanced_metadata"]["summary"]
sections = result["enhanced_metadata"]["sections"]
```

### Step 3: Query with Context
```python
# Retrieval MCP automatically uses enhanced metadata
query_result = await session.call_tool(
    "query_document",
    arguments={
        "document_id": document_id,
        "query": "What is their experience?"
    }
)

# Answer uses hierarchical context
answer = query_result["answer"]
```

---

## Testing

Run the enhanced indexing demo:
```bash
python backend/mcp_servers/client/test_enhanced_indexing.py
```

This will:
1. Upload a PDF
2. Parse and index with enhanced metadata
3. Display extracted summary and sections
4. Show hierarchical chunk structure
