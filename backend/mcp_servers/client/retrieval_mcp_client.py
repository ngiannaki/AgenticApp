# mcp_client.py
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


from mcp import ClientSession


class RetrievalMCPClient:

    def __init__(
        self,
        url: str = "http://localhost:8001/mcp",
    ):
        self.url = url

    async def query_document(
        self,
        document_id: str,
        query: str,
        top_k: int = 5,
    ):

        async with streamable_http_client(
            self.url
        ) as (read, write, _):

            async with ClientSession(
                read,
                write
            ) as session:

                await session.initialize()

                result = await session.call_tool(
                    "query_document_with_context",
                    {
                        "document_id": document_id,
                        "query": query,
                        "top_k": top_k,
                    },
                )

                return result