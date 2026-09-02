# Server Startup - Quick Reference

## Fix Applied

✅ Added automatic Python path configuration to both server files
- `backend/mcp_servers/indexing/server.py`
- `backend/mcp_servers/retrieval/server.py`

Now you can run the servers using `-m` module syntax from any working directory.

---

## Quick Commands

### Start Both Servers (PowerShell - Recommended)
```powershell
cd C:\Users\Nigia\source\repos\AgenticApp
.\start_mcp_servers.ps1
```

### Start Indexing MCP Only
```bash
cd C:\Users\Nigia\source\repos\AgenticApp
python -m backend.mcp_servers.indexing.server
```

### Start Retrieval MCP Only
```bash
cd C:\Users\Nigia\source\repos\AgenticApp
python -m backend.mcp_servers.retrieval.server
```

### Run Tests
```bash
# Full pipeline test
python backend/mcp_servers/client/test_rag_pipeline.py

# Enhanced metadata test
python backend/mcp_servers/client/test_enhanced_indexing.py

# Metadata features test
python backend/mcp_servers/client/test_metadata_features.py
```

---

## The Problem You Fixed

```
ModuleNotFoundError: No module named 'backend'
```

**Cause:** Running as a script instead of as a Python module

**Solution:** Use `python -m` syntax which automatically adds project root to Python path

---

## Key Points

| What | Command |
|------|---------|
| **Wrong** | `python backend/mcp_servers/indexing/server.py` |
| **Right** | `python -m backend.mcp_servers.indexing.server` |
| **Working Directory** | Must be project root: `C:\Users\Nigia\source\repos\AgenticApp` |
| **Indexing MCP** | Port 8000 - http://localhost:8000/mcp |
| **Retrieval MCP** | Port 8001 - http://localhost:8001/mcp |

---

## Verify It Works

1. Start indexing server:
   ```bash
   python -m backend.mcp_servers.indexing.server
   ```
   
   Should see:
   ```
   INFO:    Uvicorn running on http://0.0.0.0:8000
   ✓ Vector store initialized...
   ```

2. In another terminal, start retrieval server:
   ```bash
   python -m backend.mcp_servers.retrieval.server
   ```
   
   Should see:
   ```
   INFO:    Uvicorn running on http://0.0.0.0:8001
   ✓ DocumentRetriever initialized...
   ```

3. Run a test:
   ```bash
   python backend/mcp_servers/client/test_rag_pipeline.py
   ```
