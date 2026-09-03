import logging
from a2a.types import Role

import uvicorn
from starlette.applications import Starlette

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Part,
)
from agent import RetrievalAgent

from backend.config import settings


def create_agent_message(text: str) -> Message:
    return Message(
        role=Role.ROLE_AGENT,
        parts=[
            Part(
                text=text
            )
        ],
    )

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

HOST = settings.retrieval_agent.host
PORT = settings.retrieval_agent.port


# =========================================================
# A2A Executor
# =========================================================
class RetrievalAgentExecutor(AgentExecutor):

    def __init__(self) -> None:
        self.agent = RetrievalAgent()

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:

        question = context.get_user_input()

        if not question.strip():
            await event_queue.enqueue_event(
                create_agent_message(
                    "Please provide a question."
                )
            )
            return

        # Get metadata from the A2A request
        metadata = context.metadata or {}

        document_id = metadata.get(
            "document_id"
        )

        if not document_id:
            await event_queue.enqueue_event(
                create_agent_message(
                    "Missing document_id. "
                    "Provide document_id in the "
                    "A2A request metadata."
                )
            )
            return

        logger.info(
            "========================================"
        )
        logger.info(
            "A2A REQUEST"
        )
        logger.info(
            "document_id: %s",
            document_id,
        )
        logger.info(
            "question: %s",
            question,
        )
        logger.info(
            "========================================"
        )

        try:

            answer = await self.agent.answer_query(
                document_id=document_id,
                question=question,
            )

        except Exception as exc:

            logger.exception(
                "Agent execution failed"
            )

            answer = (
                "The retrieval agent encountered "
                f"an error: {exc}"
            )

        await event_queue.enqueue_event(
            create_agent_message(answer) # type: ignore
        )

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        pass

# =========================================================
# Main
# =========================================================

def main() -> None:

    # -----------------------------------------------------
    # Agent skill
    # -----------------------------------------------------

    skill = AgentSkill(
        id="document_retrieval",
        name="Document Retrieval",
        description=(
            "Answers questions about documents "
            "using iterative retrieval and "
            "evidence-based reasoning."
        ),
        tags=[
            "documents",
            "retrieval",
            "rag",
            "agentic-rag",
        ],
        examples=[
            "What does this document say?",
            "What are the financial consequences?",
            "What are the requirements?",
            "What does section 4 say?",
        ],
    )

    # -----------------------------------------------------
    # Agent card
    # -----------------------------------------------------

    agent_card = AgentCard(
        name="DocumentRetrievalAgent",

        description=(
            "An agentic document retrieval agent "
            "using Ollama and an MCP retrieval server."
        ),

        version="1.0.0",

        default_input_modes=[
            "text",
        ],

        default_output_modes=[
            "text",
        ],

        capabilities=AgentCapabilities(
            streaming=False,
        ),

        supported_interfaces=[
            AgentInterface(
                url=f"http://{HOST}:{PORT}/",
                protocol_binding="JSONRPC",
            ),
        ],

        skills=[
            skill,
        ],
    )

    # -----------------------------------------------------
    # A2A request handler
    # -----------------------------------------------------

    request_handler = DefaultRequestHandler(
        agent_executor=RetrievalAgentExecutor(),

        task_store=InMemoryTaskStore(),

        agent_card=agent_card,
    )

    # -----------------------------------------------------
    # Starlette application
    # -----------------------------------------------------

    routes = []

    # Agent Card:
    # /.well-known/agent-card.json
    routes.extend(
        create_agent_card_routes(
            agent_card
        )
    )

    # A2A JSON-RPC endpoint:
    # /
    routes.extend(
        create_jsonrpc_routes(
            request_handler,
            "/",
        )
    )

    app = Starlette(
        routes=routes,
    )

    # -----------------------------------------------------
    # Start server
    # -----------------------------------------------------

    logger.info(
        "Starting A2A Retrieval Agent"
    )

    logger.info(
        "Agent Card: "
        "http://%s:%s/.well-known/agent-card.json",
        HOST,
        PORT,
    )

    logger.info(
        "JSON-RPC endpoint: "
        "http://%s:%s/",
        HOST,
        PORT,
    )

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
    )


if __name__ == "__main__":
    main()