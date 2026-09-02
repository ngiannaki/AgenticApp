"""
Test enhanced metadata features including:
- Document summaries
- Section extraction
- Hierarchical chunking
- Enhanced metadata retrieval
"""
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

INDEXING_MCP_URL = "http://localhost:8000/mcp"
RETRIEVAL_MCP_URL = "http://localhost:8001/mcp"


async def demo_metadata_features(document_id: str):
    """Demonstrate enhanced metadata features."""
    
    print("\n" + "="*70)
    print("ENHANCED METADATA FEATURES DEMO")
    print("="*70)
    
    # Get document info from retrieval MCP
    print(f"\n[1/2] Retrieving document metadata...")
    
    async with streamable_http_client(RETRIEVAL_MCP_URL) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print(f"✓ Connected to Retrieval MCP")
            
            # Get document metadata
            info_result = await session.call_tool(
                "get_document_info",
                arguments={"document_id": document_id},
            )
            
            info_data = info_result.structuredContent
            
            if info_data.get("success"):
                print(f"\n✓ Document Information:")
                print(f"  Document ID: {info_data['document_id']}")
                print(f"  Document Name: {info_data.get('document_name', 'Unknown')}")
                print(f"  Total Chunks: {info_data.get('total_chunks', 0)}")
                print(f"  Total Pages: {info_data.get('total_pages', 'Unknown')}")
                
                if info_data.get("sections"):
                    print(f"\n  Sections Found: {len(info_data['sections'])}")
                    for section in info_data["sections"]:
                        print(f"    - {section}")
                
                if info_data.get("hierarchy_distribution"):
                    print(f"\n  Hierarchy Distribution:")
                    for level, count in sorted(info_data["hierarchy_distribution"].items()):
                        level_name = ["Large (2048)", "Medium (512)", "Small (128)"][int(level)]
                        print(f"    - Level {level} ({level_name}): {count} chunks")
            else:
                print(f"✗ Failed to get metadata: {info_data.get('error')}")
    
    # Query with enhanced context
    print(f"\n[2/2] Querying with enhanced metadata context...")
    
    async with streamable_http_client(RETRIEVAL_MCP_URL) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            
            #query = "What is Nikolaos Giannakis' education?"
            query = "What is this paper about and which are the best results?"
            print(f"\n  Query: {query}")
            
            query_result = await session.call_tool(
                "query_document",
                arguments={
                    "document_id": document_id,
                    "query": query
                },
            )
            
            query_data = query_result.structuredContent
            
            if query_data.get("success"):
                print(f"\n✓ Answer Generated:")
                print(f"  {query_data.get('answer')}")
                
                print(f"\n  Source Information:")
                print(f"  Chunks used: {query_data.get('chunks_count', 0)}")
                
                for i, chunk in enumerate(query_data.get('source_chunks', [])[:3], 1):
                    print(f"\n  Chunk {i}:")
                    meta = chunk.get('metadata', {})
                    print(f"    - Page: {meta.get('page_number', 'N/A')}")
                    print(f"    - Hierarchy Level: {meta.get('hierarchy_level', 'N/A')}")
                    print(f"    - Section: {meta.get('section', 'N/A')}")
                    print(f"    - Relevance Score: {chunk.get('score', 'N/A')}")
            else:
                print(f"✗ Query failed: {query_data.get('error')}")
    
    print(f"\n{'='*70}\n")


async def main():
    # Use your document_id from the enhanced indexing
    document_id = "061d7f0a-a11e-4207-94cd-6c2b06cacfee"  # Replace with your actual ID
    
    print("\nNote: Make sure you've run enhanced indexing first!")
    print(f"Using document_id: {document_id}")
    
    await demo_metadata_features(document_id)


if __name__ == "__main__":
    asyncio.run(main())
