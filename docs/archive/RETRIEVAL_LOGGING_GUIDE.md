# Retrieval Server Logging Guide

## Overview

Enhanced logging has been added to the retrieval MCP server to track:
- Tool invocations
- Query execution
- Chunk retrieval  
- LLM interactions
- Error handling

## Log Locations

### Retrieval Server Log
**Path:** `logs/retrieval_server.log`

Contains:
- Tool method calls (`query_document`, `query_document_with_context`, etc.)
- Query status (success/failure)
- Number of chunks used
- Error messages

### Indexing Server Log
**Path:** `logs/indexing_server.log`

Contains:
- Document upload info
- PDF parsing logs
- Chunk creation stats
- Vector store indexing details

## Log Format

```
2026-08-21 12:34:56,789 - backend.mcp_servers.retrieval.server - INFO - [RETRIEVAL] query_document called
```

Format: `timestamp - logger_name - level - message`

## Key Log Messages

### Query Flow

1. **Tool Called**
```
[RETRIEVAL] query_document called
[RETRIEVAL] document_id: e812da62-6d05-407d-9b14-0c1efd26bad3
[RETRIEVAL] query: What is mentioned?
```

2. **Collection Lookup**
```
[QUERY] Starting query for document e812da62-6d05-407d-9b14-0c1efd26bad3
[QUERY] Retrieving top 5 chunks
[QUERY] Collection found: doc_e812da62-6d05-407d-9b14-0c1efd26bad3
```

3. **Query Engine Setup**
```
[QUERY] Creating query engine with top_k=5
[QUERY] Executing query with LLM
```

4. **Results**
```
[QUERY] Extracting 5 source nodes
[QUERY] ✓ Query complete - 5 chunks retrieved
[RETRIEVAL] ✓ Query successful, 5 chunks used
```

### Error Flow

```
[QUERY] Collection not found: doc_invalid_uuid
[RETRIEVAL] ✗ Query failed: Document not found in vector store
```

## Log Levels

- **INFO** (✓) - Normal operations
  - Tool calls
  - Successful queries
  - Chunk retrieval counts
  
- **ERROR** (✗) - Failures
  - Document not found
  - Collection lookup failures
  - Query execution errors

## Viewing Logs

### Real-time
```bash
# Windows PowerShell
Get-Content -Path "logs/retrieval_server.log" -Wait

# Or in terminal when server is running
# Logs appear in stdout simultaneously
```

### View Complete Log
```bash
# Windows PowerShell
cat logs/retrieval_server.log

# Or
Get-Content logs/retrieval_server.log | Select-Object -Last 100  # Last 100 lines
```

### Search Logs
```bash
# Find all successful queries
Select-String "✓ Query successful" logs/retrieval_server.log

# Find all errors
Select-String "✗" logs/retrieval_server.log

# Find specific document ID
Select-String "e812da62-6d05-407d-9b14-0c1efd26bad3" logs/retrieval_server.log
```

## Console Output

Logs also appear in the terminal where the server is running:

```
2026-08-21 12:34:56,789 - backend.mcp_servers.retrieval.server - INFO - [RETRIEVAL] query_document called
2026-08-21 12:34:56,790 - backend.mcp_servers.retrieval.server - INFO - [RETRIEVAL] document_id: e812da62-6d05-407d-9b14-0c1efd26bad3
2026-08-21 12:34:56,791 - backend.mcp_servers.retrieval.server - INFO - [RETRIEVAL] query: What is mentioned?
2026-08-21 12:34:56,800 - backend.mcp_servers.retrieval.retriever - INFO - [QUERY] Starting query for document e812da62-6d05-407d-9b14-0c1efd26bad3
...
```

## Debugging Tips

### Empty Log Files?
1. Check that `logs/` directory exists
   ```bash
   ls logs/
   # Should show: indexing_server.log, retrieval_server.log
   ```

2. Ensure server is running (check terminal for startup message)
   ```
   INFO: Uvicorn running on http://0.0.0.0:8001
   ```

3. Trigger a query from test client to generate logs
   ```bash
   python backend/mcp_servers/client/test_metadata_features.py
   ```

### No Response from Server?
Check the log for errors:
```bash
Select-String "ERROR" logs/retrieval_server.log
```

### Query Not Being Executed?
Look for these lines in order:
1. `[RETRIEVAL] query_document called` - Tool was invoked
2. `[QUERY] Starting query for document` - Query execution started
3. `[QUERY] Collection found:` - Vector store collection accessed
4. `[QUERY] ✓ Query complete` - Query finished successfully

If a line is missing, that's where the issue is.

## Log Rotation

Currently logs append to single files. To rotate logs:

```bash
# Backup current logs
Move-Item logs/retrieval_server.log logs/retrieval_server.log.bak

# Server will create new log file on restart
```

## Sample Complete Log Session

```
2026-08-21 12:34:56,789 - backend.mcp_servers.retrieval.server - INFO - [RETRIEVAL] query_document called
2026-08-21 12:34:56,790 - backend.mcp_servers.retrieval.server - INFO - [RETRIEVAL] document_id: e812da62-6d05-407d-9b14-0c1efd26bad3
2026-08-21 12:34:56,791 - backend.mcp_servers.retrieval.server - INFO - [RETRIEVAL] query: Who is this person?
2026-08-21 12:34:56,800 - backend.mcp_servers.retrieval.retriever - INFO - [QUERY] Starting query for document e812da62-6d05-407d-9b14-0c1efd26bad3
2026-08-21 12:34:56,801 - backend.mcp_servers.retrieval.retriever - INFO - [QUERY] Query: Who is this person?
2026-08-21 12:34:56,802 - backend.mcp_servers.retrieval.retriever - INFO - [QUERY] Retrieving top 5 chunks
2026-08-21 12:34:56,810 - backend.mcp_servers.retrieval.retriever - INFO - [QUERY] Collection found: doc_e812da62-6d05-407d-9b14-0c1efd26bad3
2026-08-21 12:34:56,850 - backend.mcp_servers.retrieval.retriever - INFO - [QUERY] Creating query engine with top_k=5
2026-08-21 12:34:57,100 - backend.mcp_servers.retrieval.retriever - INFO - [QUERY] Executing query with LLM
2026-08-21 12:34:57,500 - backend.mcp_servers.retrieval.retriever - INFO - [QUERY] Extracting 5 source nodes
2026-08-21 12:34:57,501 - backend.mcp_servers.retrieval.retriever - INFO - [QUERY] ✓ Query complete - 5 chunks retrieved
2026-08-21 12:34:57,502 - backend.mcp_servers.retrieval.server - INFO - [RETRIEVAL] ✓ Query successful, 5 chunks used
```

## Troubleshooting

### Issue: Logs show "Collection not found"
**Solution:** Make sure document was indexed with `parse_and_index_enhanced` first
```bash
python backend/mcp_servers/client/test_enhanced_indexing.py
```

### Issue: Logs show timeout during LLM execution
**Solution:** Ensure Ollama is running:
```bash
ollama serve  # In another terminal
```

### Issue: No logs written to file
**Solution:** Check file permissions on `logs/` directory
```bash
Get-Item logs/ | Select-Object -ExpandProperty FullName
```

### Issue: Server crashes, need to debug
**Solution:** Look at last lines of log file
```bash
Get-Content logs/retrieval_server.log | Select-Object -Last 20
```
