"""The agents service: every registered agent as an A2A server.

    python -m backend.apps.agents.main

Per agent: A2A JSON-RPC at /agents/{id}, card at
/agents/{id}/.well-known/agent-card.json. GET /agents lists all agents with
their input forms (what the API and frontend render). Every request must
carry the internal token: only the API talks to this service.
"""
from __future__ import annotations

import logging
import secrets

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from backend.apps.agents.base import AgentExecutorBridge, BaseAgent
from backend.apps.agents.registry import AGENTS
from backend.config import settings

logger = logging.getLogger("agents")


def agent_card(agent: BaseAgent, base_url: str) -> AgentCard:
    return AgentCard(
        name=agent.name,
        description=agent.description,
        version="1.0.0",
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=f"{base_url}/agents/{agent.id}/")],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["application/json"],
        default_output_modes=["text/markdown", "application/json"],
        skills=[AgentSkill(id=agent.id, name=agent.name, description=agent.description, tags=["agent"], examples=agent.examples)],
    )


class InternalTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        expected = settings.agents.internal_token or ""
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not expected or not secrets.compare_digest(given, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def create_app(agents: list[BaseAgent] = AGENTS, base_url: str | None = None) -> Starlette:
    base_url = base_url or settings.agents.url
    routes: list = [Route("/agents", lambda request: JSONResponse([a.describe() for a in agents]))]
    for agent in agents:
        card = agent_card(agent, base_url)
        handler = DefaultRequestHandler(agent_executor=AgentExecutorBridge(agent), task_store=InMemoryTaskStore(), agent_card=card)
        routes.append(Mount(f"/agents/{agent.id}", routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/")))
    app = Starlette(routes=routes)
    app.add_middleware(InternalTokenMiddleware)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not settings.agents.internal_token:
        raise SystemExit("AGENTS_INTERNAL_TOKEN is not set — the agents service won't start without it.")
    uvicorn.run(create_app(), host=settings.agents.host, port=settings.agents.port)


if __name__ == "__main__":
    main()
