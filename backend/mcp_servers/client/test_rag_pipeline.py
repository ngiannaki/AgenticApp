"""
Complete end-to-end test for the RAG pipeline:
1. Upload a PDF
2. Parse the PDF
3. Index the parsed content
4. Query the indexed document
"""
import asyncio
import base64
import json
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

INDEXING_MCP_URL = "http://localhost:8000/mcp"
RETRIEVAL_MCP_URL = "http://localhost:8001/mcp"


async def run_indexing_pipeline(pdf_path: str):
    """Upload, parse, and index a PDF document."""
    
    print(f"\n{'='*60}")
    print("INDEXING PIPELINE")
    print(f"{'='*60}")
    
    async with streamable_http_client(INDEXING_MCP_URL) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print(f"✓ Connected to Indexing MCP (port 8000)")

            # 1. Upload
            print(f"\n[1/3] Uploading PDF...")
            
            # Use file_path instead of base64 to avoid "413 Content Too Large" error
            upload_result = await session.call_tool(
                "upload_document",
                arguments={"file_path": pdf_path},
            )
            
            upload_data = upload_result.structuredContent
            if not upload_data.get("success"):
                print(f"✗ Upload failed: {upload_data.get('error')}")
                return None
            
            document_id = upload_data["document_id"]
            print(f"✓ Document uploaded: {document_id}")

            # 2. Parse
            print(f"\n[2/3] Parsing PDF...")
            parse_result = await session.call_tool(
                "parse_document",
                arguments={"document_id": document_id},
            )
            
            parse_data = parse_result.structuredContent
            print(f"✓ Document parsed: {parse_data.get('page_count', 0)} pages")

            # 3. Index
            print(f"\n[3/3] Indexing parsed content into vector store...")
            parsed_json = json.dumps(parse_data)
            
            index_result = await session.call_tool(
                "index_document",
                arguments={
                    "document_id": document_id,
                    "parsed_content": parsed_json
                },
            )
            
            index_data = index_result.structuredContent
            if not index_data.get("success"):
                print(f"✗ Indexing failed: {index_data.get('error')}")
                return None
            
            print(f"✓ Document indexed: {index_data.get('chunks_indexed', 0)} chunks")
            print(f"  Path: {index_data.get('persist_path')}")
            
            return document_id


async def run_retrieval_pipeline(document_id: str, query: str):
    """Query the indexed document."""
    
    print(f"\n{'='*60}")
    print("RETRIEVAL PIPELINE (RAG)")
    print(f"{'='*60}")
    
    async with streamable_http_client(RETRIEVAL_MCP_URL) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print(f"✓ Connected to Retrieval MCP (port 8001)")

            print(f"\n📝 Query: {query}")
            print(f"📄 Document ID: {document_id}")
            print("-" * 60)
            
            query_result = await session.call_tool(
                "query_document",
                arguments={
                    "document_id": document_id,
                    "query": query
                },
            )

            query_data = query_result.structuredContent
            
            if not query_data.get("success"):
                print(f"✗ Query failed: {query_data.get('error')}")
                return

            print(f"\n💡 ANSWER:")
            print(f"{query_data.get('answer')}")
            
            print(f"\n📚 SOURCE CHUNKS ({query_data.get('chunks_count')} used):")
            print("-" * 60)
            for i, chunk in enumerate(query_data.get('source_chunks', []), 1):
                print(f"\nChunk {i}:")
                print(f"  Score: {chunk.get('score'):.3f}" if chunk.get('score') else "  Score: N/A")
                print(f"  Page: {chunk.get('metadata', {}).get('page_number', 'N/A')}")
                text_preview = chunk.get('text', '')[:150].replace('\n', ' ')
                print(f"  Text: {text_preview}...")


async def main():
    print("\n" + "="*60)
    print("RAG SYSTEM END-TO-END TEST")
    print("="*60)
    
    # Configuration
    pdf_path = r"C:\Users\Nigia\source\repos\AgenticApp\Profile.pdf"
    
    # Check if PDF exists
    if not Path(pdf_path).exists():
        print(f"\n✗ PDF not found at: {pdf_path}")
        print("  Please update the pdf_path variable in this script")
        return
    
    # Run indexing pipeline
    document_id = await run_indexing_pipeline(pdf_path)
    
    if not document_id:
        print("\n✗ Indexing pipeline failed")
        return
    
    # Run retrieval pipeline with multiple queries
    queries = [
        "Who is Nikolaos Giannakis?",
        "What is mentioned about the person?",
        "What are the key details?",
    ]
    
    for query in queries:
        await run_retrieval_pipeline(document_id, query)
        print("\n" + "-"*60)
    
    print(f"\n✓ Pipeline complete! Document ID: {document_id}")


if __name__ == "__main__":
    asyncio.run(main())
