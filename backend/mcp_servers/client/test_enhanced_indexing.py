"""
Enhanced Metadata Features Demo

Shows how to use the enhanced indexing with:
1. Hierarchical chunking
2. Document summaries
3. Section extraction
4. Document statistics
5. Enhanced metadata
"""
import asyncio
import base64
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

INDEXING_MCP_URL = "http://localhost:8000/mcp"


async def demo_enhanced_indexing(pdf_path: str):
    """Demonstrate enhanced indexing features."""
    
    print("\n" + "="*70)
    print("ENHANCED METADATA INDEXING DEMO")
    print("="*70)
    
    async with streamable_http_client(INDEXING_MCP_URL) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print(f"✓ Connected to Indexing MCP")

            # Check PDF exists
            if not Path(pdf_path).exists():
                print(f"✗ PDF not found at: {pdf_path}")
                return

            print(f"\n[1/2] Uploading PDF...")
            
            # Use file_path instead of base64 to avoid "413 Content Too Large" error
            upload_result = await session.call_tool(
                "upload_document",
                arguments={
                    "file_path": pdf_path  # Use file path directly
                },
            )
            
            upload_data = upload_result.structuredContent
            if not upload_data.get("success"):
                print(f"✗ Upload failed: {upload_data.get('error')}")
                return
            
            document_id = upload_data["document_id"]
            print(f"✓ Document uploaded: {document_id}")

            # Enhanced indexing with metadata extraction
            print(f"\n[2/2] Enhanced parsing and indexing...")
            print("  - Hierarchical chunking enabled")
            print("  - Metadata extraction enabled")
            print("  - Document summary generation")
            print("  - Section extraction")
            
            enhanced_result = await session.call_tool(
                "parse_and_index_enhanced",
                arguments={
                    "document_id": document_id,
                    "document_name": Path(pdf_path).name,
                    "use_hierarchical": True,
                },
            )
            
            result_data = enhanced_result.structuredContent
            
            print(f"\n{'='*70}")
            print("INDEXING RESULTS")
            print(f"{'='*70}")
            
            if result_data.get("success"):
                print(f"\n✓ Indexing successful!")
                print(f"\nBasic Info:")
                print(f"  Document ID: {result_data.get('document_id')}")
                print(f"  Chunks indexed: {result_data.get('chunks_indexed')}")
                print(f"  Collection: {result_data.get('collection_name')}")
                print(f"  Persist path: {result_data.get('persist_path')}")
                
                # Enhanced metadata
                enhanced_meta = result_data.get("enhanced_metadata", {})
                
                if enhanced_meta:
                    print(f"\nEnhanced Metadata:")
                    
                    if enhanced_meta.get("document_name"):
                        print(f"  Document Name: {enhanced_meta['document_name']}")
                    
                    if enhanced_meta.get("word_count"):
                        print(f"  Word Count: {enhanced_meta['word_count']}")
                    
                    if enhanced_meta.get("char_count"):
                        print(f"  Character Count: {enhanced_meta['char_count']}")
                    
                    if enhanced_meta.get("summary"):
                        print(f"\n  Summary:")
                        print(f"    {enhanced_meta['summary']}")
                    
                    if enhanced_meta.get("sections"):
                        print(f"\n  Extracted Sections ({len(enhanced_meta['sections'])}):")
                        for i, section in enumerate(enhanced_meta["sections"], 1):
                            print(f"    {i}. {section.get('title', 'Untitled')}")
                            if section.get('description'):
                                desc = section['description']
                                desc = desc[:80] + "..." if len(desc) > 80 else desc
                                print(f"       {desc}")
            else:
                print(f"✗ Indexing failed!")
                print(f"  Error: {result_data.get('error')}")
            
            print(f"\n{'='*70}")
            print(f"Document ID for querying: {document_id}")
            print(f"Use this ID with the retrieval MCP to query the document")
            print(f"{'='*70}\n")


async def main():
    pdf_path = r"C:\\Users\\Nigia\\source\\repos\\AgenticApp\\paper1.pdf"
    await demo_enhanced_indexing(pdf_path)


if __name__ == "__main__":
    asyncio.run(main())
