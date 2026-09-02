"""
Generic MCP Agent Client - Auto Tool Orchestration
===================================================

A simple, generic agent that:
- Takes a natural language prompt
- Automatically uses available tools to complete the task
- Runs up to 10 iterations (configurable)
- Handles multi-step workflows end-to-end

Example Prompts:
    "I have a PDF at /path/to/document.pdf. Please upload it, index it, and tell me what's in it."
    "Upload and index my research paper, then summarize the main findings."
    "What tools are available for document processing?"

Usage:
    python backend/apps/client/example_mcp_client.py "Your prompt here"

Or programmatically:
    from example_mcp_client import run_agent_task
    result = asyncio.run(run_agent_task("Upload and index my PDF document"))
"""
import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.apps.client.agent import MCPAgent
from backend.apps.client.config import AgentConfig


async def run_agent_task(prompt: str, max_iterations: int = 10, log_level: str = "INFO") -> str:
    """
    Run an agent task with the given prompt.

    The agent will:
    1. Connect to all available MCP servers (indexing, retrieval, etc.)
    2. Load all available tools
    3. Autonomously decide which tools to use based on the prompt
    4. Execute up to max_iterations to complete the task
    5. Return the final answer

    Args:
        prompt: Natural language description of what you want the agent to do
        max_iterations: Maximum number of tool-calling iterations (default: 10)
        log_level: Logging level - "DEBUG", "INFO", "WARNING", "ERROR" (default: "INFO")

    Returns:
        The agent's final answer as a string

    Example:
        result = await run_agent_task(
            "Upload the PDF at /tmp/document.pdf and index it"
        )
        print(result)
    """
    print("\n" + "=" * 70)
    print("🤖 Generic MCP Agent - Auto Tool Orchestration")
    print("=" * 70)
    print(f"\n📝 Prompt: {prompt}\n")

    # Load config with custom settings
    config = AgentConfig.load(
        max_iterations=max_iterations,
        log_level=log_level,
        system_prompt=(
            # "You are a document analysis assistant. Your job is to:\n"
            # "1. Use tools to retrieve document chunks\n"
            # "2. ANALYZE the chunks returned by tools\n"
            # "3. SYNTHESIZE clear answers from the retrieved chunks\n"
            # "\nWhen you receive tool results with chunks:\n"
            # "- READ the chunk content carefully\n"
            # "- EXTRACT the relevant information\n"
            # "- ANSWER the user's question based on chunk content\n"
            # "- CITE which chunks had the information\n"
            # "\nAvailable tools:\n"
            # "- retrieval_retrieve_document(document_id, query, top_k): Returns chunks from indexed documents\n"
            # "- retrieval_get_document_info(document_id): Returns document metadata\n"
            # "- indexing_upload_document(file_path): Uploads and indexes a PDF\n"
            # "- indexing_parse_and_index_enhanced(document_id, document_name, use_hierarchical): Indexes uploaded docs\n"
            # "\nCRITICAL RULES:\n"
            # "1. When tool returns chunks (source_chunks array): READ THEM and answer immediately\n"
            # "2. Extract exact text from chunks to answer user questions\n"
            # "3. DO NOT ask for files if chunks are already retrieved\n"
            # "4. DO NOT return empty responses - always provide answers from chunks\n"
            # "5. For questions about 'top models': look for table data or ranked lists in chunks\n"
            # "6. For 'what is document about': summarize from the chunks content\n"
            # "7. Call tools multiple times with different queries if needed to find all information"
            "Get the user answer and the available tools. " \
            "Analyze your steps so as to complete the task. " \
            "Use the tools so as to complete your goal."
        )
    )

    try:
        async with MCPAgent(config) as agent:
            print(f"✓ Connected to {len(agent.tool_names)} tools")
            print(f"  Tools: {', '.join(agent.tool_names)}\n")

            # Run the agent with the prompt
            answer = await agent.run(prompt)

            print("\n" + "=" * 70)
            print("✅ Task Complete")
            print("=" * 70)
            return answer

    except Exception as e:
        error_msg = f"Agent error: {str(e)}"
        print(f"\n❌ {error_msg}")
        raise


async def interactive_mode():
    """
    Interactive mode - prompt the user for input and run agent
    """
    print("\n" + "=" * 70)
    print("🤖 Generic MCP Agent - Interactive Mode")
    print("=" * 70)
    print("\nAvailable tools:")
    config = AgentConfig.load()

    async with MCPAgent(config) as agent:
        print(f"  • {', '.join(agent.tool_names)}")

        print("\n" + "-" * 70)
        print("Enter your prompt (or 'quit' to exit):")
        print("-" * 70)

        while True:
            try:
                prompt = input("\n>>> ").strip()

                if not prompt:
                    continue

                if prompt.lower() in ("quit", "exit", "q"):
                    print("\n👋 Goodbye!")
                    break

                # Run the agent
                answer = await agent.run(prompt)
                print(f"\n📌 Answer:\n{answer}")

            except KeyboardInterrupt:
                print("\n\n👋 Interrupted. Goodbye!")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")


def main():
    """Main entry point"""

    # Check if prompt provided as command-line argument
    if len(sys.argv) > 1:
        # Join all arguments to form the prompt
        prompt = " ".join(sys.argv[1:])
        asyncio.run(run_agent_task(prompt))
    else:
        # Show usage info and examples
        print("\n" + "=" * 70)
        print("🤖 Generic MCP Agent Client - Auto Tool Orchestration")
        print("=" * 70)

        print("\n📋 USAGE:")
        print("  python backend/apps/client/example_mcp_client.py \"<your prompt here>\"")

        print("\n💡 EXAMPLES:")
        examples = [
            "Upload and index the PDF at /path/to/document.pdf",
            "I have a research paper at ./research.pdf. Index it and summarize the findings.",
            "What tools are available for document processing?",
            "Upload my PDF from C:/docs/report.pdf and tell me what's in it",
        ]
        for i, example in enumerate(examples, 1):
            print(f"  {i}. python backend/apps/client/example_mcp_client.py \"{example}\"")

        print("\n🔧 FEATURES:")
        print("  • Automatically connects to all MCP servers")
        print("  • Loads all available tools dynamically")
        print("  • Uses up to 10 iterations to complete tasks")
        print("  • Handles multi-step workflows end-to-end")
        print("  • No manual tool selection needed - agent decides!")

        print("\n📚 AVAILABLE TOOLS:")
        config = AgentConfig.load()
        print("  Servers configured in servers.json:")
        for server, url_config in config.servers.get("mcpServers", {}).items():
            url = url_config.get("url", "N/A")
            print(f"    • {server}: {url}")

        print("\n🚀 TRY IT NOW:")
        print("  python backend/apps/client/example_mcp_client.py \"Upload the PDF at ./docs/sample.pdf and index it\"")

        print("\n🔄 INTERACTIVE MODE:")
        print("  python -c \"import asyncio; from backend.apps.client.example_mcp_client import interactive_mode; asyncio.run(interactive_mode())\"")

        print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    main()
