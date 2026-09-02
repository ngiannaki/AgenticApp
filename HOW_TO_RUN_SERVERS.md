# How to Run the MCP Servers

## Quick Start (Recommended)

### Option 1: Using PowerShell Script (Windows)
```powershell
# Make sure you're in the project root
cd C:\Users\Nigia\source\repos\AgenticApp

# Run the startup script
.\start_mcp_servers.ps1
```

This will start both servers in separate windows.

### Option 2: Using Python Script
```bash
# Make sure you're in the project root
cd C:\Users\Nigia\source\repos\AgenticApp

# Run the Python startup script
python start_mcp_servers.py
```

### Option 3: Manual - Start Each Server Separately

**Terminal 1 - Indexing MCP (Port 8000):**
```bash
cd C:\Users\Nigia\source\repos\AgenticApp
python -m backend.mcp_servers.indexing.server
```

**Terminal 2 - Retrieval MCP (Port 8001):**
```bash
cd C:\Users\Nigia\source\repos\AgenticApp
python -m backend.mcp_servers.retrieval.server
```

---

## Important: Python Module Syntax

⚠️ **DO NOT run scripts directly:**
```bash
# ❌ WRONG - Will cause ModuleNotFoundError
python backend/mcp_servers/indexing/server.py
```

✅ **ALWAYS use module syntax:**
```bash
# ✓ CORRECT - Adds project root to Python path
python -m backend.mcp_servers.indexing.server
```

**Why?** The server files use absolute imports like `from backend.core.storage...`. When you run a script with `-m`, Python automatically adds the parent directory to sys.path, allowing these imports to work.

---

## Running from Any Directory

You can run the servers from any directory by providing the full path:

```bash
cd C:\Users\Some\Other\Path

# Run indexing server
python -m backend.mcp_servers.indexing.server -C C:\Users\Nigia\source\repos\AgenticApp

# Or just ensure PYTHONPATH is set
set PYTHONPATH=C:\Users\Nigia\source\repos\AgenticApp
python -m backend.mcp_servers.indexing.server
```

---

## Testing the Servers

Once both servers are running, test them with:

### Test Indexing MCP
```bash
python backend/mcp_servers/client/test_mcp_client.py
```

### Test Retrieval MCP
```bash
python backend/mcp_servers/client/test_mcp_client_retrieval.py
```

### Full RAG Pipeline
```bash
python backend/mcp_servers/client/test_rag_pipeline.py
```

### Enhanced Metadata Features
```bash
python backend/mcp_servers/client/test_enhanced_indexing.py
```

---

## Server URLs

- **Indexing MCP:** `http://localhost:8000/mcp`
- **Retrieval MCP:** `http://localhost:8001/mcp`

---

## Troubleshooting

### ModuleNotFoundError: No module named 'backend'

**Problem:** Running script directly instead of as module

**Solution:** Always use `python -m backend.mcp_servers.indexing.server` instead of `python backend/mcp_servers/indexing/server.py`

### ImportError: No module named 'backend.core.storage'

**Problem:** Python path not configured correctly

**Solution:** 
1. Run from project root directory
2. Use `python -m` syntax
3. Or set `PYTHONPATH=C:\Users\Nigia\source\repos\AgenticApp`

### Connection refused on port 8000/8001

**Problem:** Servers not running

**Solution:** Start servers first:
```bash
python -m backend.mcp_servers.indexing.server
python -m backend.mcp_servers.retrieval.server
```

### Port already in use

**Problem:** Another process is using the port

**Solution:** 
```bash
# Find and kill process on port 8000
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# Or change port in server.py:
# mcp.run(..., port=8002)
```

---

## Environment Setup

Make sure your virtual environment is activated:

```bash
# Activate venv (Windows)
.venv\Scripts\Activate.ps1

# Or (Command Prompt)
.venv\Scripts\activate.bat

# Verify Python path
python -c "import sys; print(sys.path)"
```

---

## Logs

Server logs are saved to:
- `logs/indexing_server.log` - Indexing MCP logs
- `logs/retrieval_server.log` - Retrieval MCP logs

Check logs if servers have issues:
```bash
cat logs/indexing_server.log
```
