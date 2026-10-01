# HTTP 413 Content Too Large - FIXED

## Problem
When uploading large PDF files via MCP's HTTP transport, the request payload becomes too large (base64 encoding increases size by ~33%), causing:
```
httpx.HTTPStatusError: Client error '413 Content Too Large'
```

## Root Cause
- PDF files encoded as base64 strings → Significantly larger payloads
- HTTP request size limits in MCP/Starlette/httpx
- Example: A 2MB PDF becomes ~2.7MB when base64 encoded

## Solution

### Updated `upload_document` Tool
Now accepts **either**:
```python
# Option 1: File path (RECOMMENDED for large files)
upload_result = await session.call_tool(
    "upload_document",
    arguments={"file_path": "/path/to/document.pdf"}
)

# Option 2: Base64 encoding (OK for small files only)
upload_result = await session.call_tool(
    "upload_document",
    arguments={"pdf_base64": "JVBERi0xLjQK..."}
)
```

### Why File Path is Better
✓ **No encoding overhead** - Direct file access on server  
✓ **Larger file support** - No HTTP request size limits  
✓ **Faster upload** - No encoding/decoding needed  
✓ **More efficient** - Less network traffic  

## Changes Made

### Backend (Server)
**File:** `backend/mcp_servers/indexing/server.py`

```python
@mcp.tool
def upload_document(pdf_base64: str = None, file_path: str = None) -> dict:
    """
    Upload a PDF document to the server.
    
    Can accept either:
    - pdf_base64: Base64 encoded PDF (small files)
    - file_path: Local file path to PDF (large files, recommended)
    """
```

Added support for both input methods with fallback logic.

### Test Clients

All test clients updated to use `file_path`:

**Files Updated:**
- `test_mcp_client.py`
- `test_enhanced_indexing.py`
- `test_rag_pipeline.py`

**Before:**
```python
pdf_bytes = open(pdf_path, "rb").read()
pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
upload_result = await session.call_tool(
    "upload_document",
    arguments={"pdf_base64": pdf_base64}
)
```

**After:**
```python
upload_result = await session.call_tool(
    "upload_document",
    arguments={"file_path": pdf_path}
)
```

## How to Use

### With File Path (Recommended)
```bash
# Directly reference the PDF file
python backend/mcp_servers/client/test_enhanced_indexing.py
```

The server will:
1. Receive the file path
2. Open and read the file directly
3. Store in document store
4. Continue with parse/index

### With Base64 (Small Files Only)
For files < 10MB, you can still use base64 encoding:

```python
with open("small.pdf", "rb") as f:
    pdf_bytes = f.read()
pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')

upload_result = await session.call_tool(
    "upload_document",
    arguments={"pdf_base64": pdf_base64}
)
```

## Return Value

```python
{
    "success": True,
    "document_id": "e812da62-6d05-407d-9b14-0c1efd26bad3",
    "file_size": 1234567,  # Bytes stored
    "message": "Document uploaded successfully with ID: ..."
}
```

## Testing

Run the enhanced indexing demo:
```bash
python backend/mcp_servers/client/test_enhanced_indexing.py
```

Or the full RAG pipeline:
```bash
python backend/mcp_servers/client/test_rag_pipeline.py
```

Both now use `file_path` parameter to avoid 413 errors.

## Backward Compatibility

✓ Existing code using `pdf_base64` still works (for small files)  
✓ No breaking changes to tool signature  
✓ Both parameters are optional (one must be provided)  

## File Size Limits

- **File path**: Unlimited (filesystem limit)
- **Base64**: ~100MB practical limit before HTTP timeouts
- **Recommended**: Use file path for anything > 10MB
