import asyncio
import base64
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pathlib import Path

MCP_URL = "http://localhost:8000/mcp"


async def main():

    print(f"Connecting to {MCP_URL}...")
    
    async with streamable_http_client(MCP_URL) as (
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

            print("Connected to MCP server")

            # --------------------------------
            # List tools
            # --------------------------------

            tools = await session.list_tools()

            print("\nAvailable tools:")

            for tool in tools.tools:
                print(f"  - {tool.name}")

            # --------------------------------
            # Upload a document
            # --------------------------------

            pdf_path = r"C:\\Users\\Nigia\\source\\repos\\AgenticApp\\Profile.pdf"
            
            if Path(pdf_path).exists():
                print(f"\nUploading document from: {pdf_path}")
                
                # Use file_path instead of base64 to avoid "413 Content Too Large" error
                upload_result = await session.call_tool(
                    "upload_document",
                    arguments={
                        "file_path": pdf_path
                    },
                )

                print(f"Upload result: {upload_result}")

                # Extract the structured content from CallToolResult
                upload_data = upload_result.structuredContent
                
                if upload_data and upload_data.get("success"):
                    document_id = upload_data["document_id"]
                    
                    # --------------------------------
                    # Parse the uploaded document
                    # --------------------------------
                    
                    # print(f"\nParsing document: {document_id}")
                    
                    # parse_result = await session.call_tool(
                    #     "parse_document",
                    #     arguments={
                    #         "document_id": document_id
                    #     },
                    # )

                    # parse_data = parse_result.structuredContent
                    # print(f"Parsed {parse_data.get('page_count', 0)} pages")
                    
                    # --------------------------------
                    # Index the parsed document
                    # --------------------------------
                    
                    print(f"\nIndexing document: {document_id}")
                    
                    # Convert parsed content to JSON string for transmission
                    # parsed_json = json.dumps(parse_data)
                    
                    index_result = await session.call_tool(
                        "parse_and_index",
                        arguments={
                            "document_id": document_id,
                        },
                    )
                    
                    index_data = index_result.structuredContent
                    print(f"\nIndexing result:")
                    print(f"  Success: {index_data.get('success')}")
                    print(f"  Chunks indexed: {index_data.get('chunks_indexed', 0)}")
                    if index_data.get('message'):
                        print(f"  Message: {index_data.get('message')}")
            else:
                print(f"PDF file not found at: {pdf_path}")


if __name__ == "__main__":
    asyncio.run(main())