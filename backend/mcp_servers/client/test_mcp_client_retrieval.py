import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

RETRIEVAL_MCP_URL = "http://localhost:8001/mcp"


async def main():

    print(f"Connecting to {RETRIEVAL_MCP_URL}...")
    
    async with streamable_http_client(RETRIEVAL_MCP_URL) as (
        read_stream,
        write_stream,
        _,
    ):

        async with ClientSession(
            read_stream,
            write_stream,
        ) as session:

            # --------------------------------
            # Initialize MCP session
            # --------------------------------

            await session.initialize()

            print("Connected to Retrieval MCP server")

            # --------------------------------
            # List tools
            # --------------------------------

            tools = await session.list_tools()

            print("\nAvailable tools:")

            for tool in tools.tools:
                print(f"  - {tool.name}")

            # Use the document ID from your uploaded document
            # You can get this from the indexing workflow
            document_id = "c7e2797c-a8b7-4306-8cad-1b3ed5f18978"  # Replace with your actual document_id
            query = "What is the education of Nikolaos Giannakis?"
            
            print(f"\n{'='*50}")
            print(f"Querying document: {document_id}")
            print(f"Query: {query}")
            print(f"{'='*50}")
            
            # --------------------------------
            # Query the document
            # --------------------------------
            
            query_result = await session.call_tool(
                "query_document",
                arguments={
                    "document_id": document_id,
                    "query": query
                },
            )

            query_data = query_result.structuredContent
            
            if query_data.get("success"):
                print(f"\n✓ Query successful!")
                print(f"\nAnswer:")
                print(f"{query_data.get('answer')}")
                
                print(f"\n\nSource Chunks ({query_data.get('chunks_count')} used):")
                print("-" * 50)
                for i, chunk in enumerate(query_data.get('source_chunks', []), 1):
                    print(f"\nChunk {i}:")
                    print(f"  Score: {chunk.get('score')}")
                    print(f"  Metadata: {chunk.get('metadata')}")
                    print(f"  Text: {chunk.get('text')[:200]}...")
            else:
                print(f"\n✗ Query failed!")
                print(f"  Error: {query_data.get('error')}")


if __name__ == "__main__":
    asyncio.run(main())
