# Generic MCP Agent Client - Auto Tool Orchestration

A simplified, generic agent that automatically handles document processing tasks end-to-end.

## How It Works

1. **You provide a prompt** - Describe what you want to do in natural language
2. **Agent analyzes the prompt** - Determines which tools are needed
3. **Agent executes tools** - Automatically calls tools in sequence up to 10 iterations
4. **Agent returns result** - Provides the final answer

**No manual tool selection needed!** The agent figures it all out.

## Prerequisites

Start the MCP servers:
```powershell
.\start_mcp_servers.ps1
```

This starts:
- Indexing server on `http://localhost:8000/mcp` (upload, parse, index documents)
- Retrieval server on `http://localhost:8001/mcp` (query, retrieve, summarize documents)

## Quick Start - CLI Usage

### Show Help
```bash
# From project root (AgenticApp)
python backend/apps/client/example_mcp_client.py
```

### Run a Task
```bash
# Example 1: Upload and index a document
python backend/apps/client/example_mcp_client.py "Upload and index the PDF at C:/docs/research.pdf"

# Example 2: Upload, index, and get info
python backend/apps/client/example_mcp_client.py "I have a PDF at ./paper.pdf. Upload it, index it, and tell me what's inside."

# Example 3: Query a document (if already indexed)
python backend/apps/client/example_mcp_client.py "Query document abc123def456 for information about machine learning"

# Example 4: Ask about available tools
python backend/apps/client/example_mcp_client.py "What document processing tools are available?"
```

## Programmatic Usage

Use it in your Python code:

```python
import asyncio
from backend.apps.client.example_mcp_client import run_agent_task

async def main():
    result = await run_agent_task(
        "Upload the PDF at /path/to/document.pdf and index it"
    )
    print(result)

asyncio.run(main())
```

With custom settings:
```python
result = await run_agent_task(
    prompt="Find the main topic of my PDF at ./research.pdf",
    max_iterations=15,      # Up to 15 iterations
    log_level="DEBUG"       # Detailed logging
)
```

## Available Tools

### Indexing Server
| Tool | Purpose |
|------|---------|
| `upload_document` | Upload PDF via file path or base64 |
| `upload_document_bytes` | Upload PDF as raw bytes |
| `parse_and_index` | Parse and index in one step |
| `parse_and_index_enhanced` | Parse with metadata extraction (summaries, sections, etc.) |

### Retrieval Server
| Tool | Purpose |
|------|---------|
| `query_document_with_context` | Query document with configurable context |
| `retrieve_document` | Retrieve relevant chunks only |
| `get_document_info` | Get document metadata and statistics |
| `summarize_document` | Summarize using a query as context |

## Example Prompts

The agent can handle natural language like:

```
"I have a PDF at /home/user/research.pdf. Please upload it to the system, index it, 
and then tell me the top 3 main topics discussed in the document."

"Upload the document at C:\Documents\report.pdf, get document info about it, 
and provide a summary."

"What's the document processing workflow? What tools are available?"

"I need to analyze multiple PDFs. What's the best approach using the available tools?"
```

## How the Agent Works (Behind the Scenes)

1. **Initialization**
   - Connects to all configured MCP servers (indexing, retrieval, etc.)
   - Discovers all available tools
   - Loads system prompt and configuration

2. **Tool-Calling Loop** (up to 10 iterations)
   - Sends your prompt + available tools to the LLM
   - LLM decides which tool(s) to call next
   - Agent executes the tool calls
   - LLM sees the results and decides next steps
   - Repeats until the task is complete

3. **Completion**
   - When LLM has enough information, it returns a final answer
   - Agent outputs the result

## Configuration

Edit `config.py` to customize:

```python
# System prompt for the agent
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant with access to document processing tools..."

# Maximum iterations
max_iterations: int = 10

# Ollama settings
ollama_base_url: str = "http://localhost:11434"
ollama_model: str = "mistral"

# Temperature (0-1, lower = more deterministic)
temperature: float = 0.2

# Conversation history size
max_history_messages: int = 40
```

## Server Configuration

Edit `servers.json` to add/modify servers:

```json
{
  "mcpServers": {
    "indexing": {
      "url": "http://localhost:8000/mcp"
    },
    "retrieval": {
      "url": "http://localhost:8001/mcp"
    }
  }
}
```

## Debugging

### Check Server Status
```bash
# Test if servers are responding
curl http://localhost:8000/mcp
curl http://localhost:8001/mcp
```

### View Logs
```bash
# Windows PowerShell - watch logs in real-time
Get-Content logs/indexing_server.log -Tail 50 -Wait
Get-Content logs/retrieval_server.log -Tail 50 -Wait
```

### Enable Debug Logging
```bash
# Run with debug output
python backend/apps/client/example_mcp_client.py "your prompt" --debug
```

Or in code:
```python
await run_agent_task(prompt, log_level="DEBUG")
```

## Workflow Example

### Upload and Index a Document
```
User Input:
  "Upload and index the PDF at C:/docs/research.pdf"

Agent Steps:
  1. Call upload_document with file path
  2. Receive document_id
  3. Call parse_and_index_enhanced with document_id
  4. Return success message with document_id
```

### Query a Document
```
User Input:
  "Query document abc123 about machine learning findings"

Agent Steps:
  1. Call get_document_info to verify document exists
  2. Call query_document_with_context with the query
  3. Return answer with source chunks
```

### Complete Analysis
```
User Input:
  "Upload research.pdf, index it, and summarize the key findings"

Agent Steps:
  1. Call upload_document with file path
  2. Call parse_and_index_enhanced with document_id
  3. Call get_document_info to understand structure
  4. Call summarize_document with a query
  5. Return comprehensive summary
```

## Troubleshooting

### "Connection refused" Error
- Make sure servers are running: `.\start_mcp_servers.ps1`
- Check they're on correct ports: `localhost:8000` and `localhost:8001`

### Agent Doesn't Find Tools
- Verify servers.json has correct URLs with `/mcp` path
- Check log output shows tools being loaded
- Ensure servers are responding to requests

### Agent Times Out or Doesn't Complete
- Increase `max_iterations` in code
- Use simpler, more specific prompts
- Check logs for tool call errors

### LLM/Ollama Issues
- Verify Ollama is running
- Check `.env` has correct `OLLAMA_BASE_URL` and `OLLAMA_MODEL`
- Test Ollama: `ollama list` or visit `http://localhost:11434`

## Architecture

```
┌─────────────────────────────────┐
│   User Prompt                   │
│  (natural language request)     │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│   MCPAgent                      │
│  (orchestrator)                 │
└────────────┬────────────────────┘
             │
      ┌──────┴──────┐
      │             │
      ▼             ▼
┌────────────┐  ┌────────────┐
│ Indexing   │  │ Retrieval  │
│ Server     │  │ Server     │
│ (8000)     │  │ (8001)     │
└─────┬──────┘  └─────┬──────┘
      │              │
      ▼              ▼
 Upload/Index   Query/Retrieve
 Documents      Chunks
```

## Files

- `example_mcp_client.py` - Main generic agent client
- `agent.py` - MCPAgent implementation (tool-calling loop)
- `mcp_client.py` - Low-level MCP client for direct tool calls
- `config.py` - Configuration for agent behavior
- `servers.json` - Server endpoint mapping
- `EXAMPLE_README.md` - This file
