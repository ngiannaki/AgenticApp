# server.py
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handler import DefaultRequestHandler
from .agent import RetrievalExecutor

agent = A2AStarletteApplication(
    http_handler=DefaultRequestHandler(
        agent_executor=RetrievalExecutor(),
    )
)

app = agent.build()