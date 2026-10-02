"""The agents this service runs. To add one: write a BaseAgent subclass
(see base.py) and append an instance here."""
from __future__ import annotations

from backend.apps.agents.analyst import DataAnalystAgent
from backend.apps.agents.base import BaseAgent
from backend.apps.agents.custom import CustomAgent
from backend.apps.agents.extractor import ExtractorAgent
from backend.apps.agents.research import DeepResearchAgent

# CustomAgent ("custom") runs the agents users build on the Agents page; the
# API starts it with the user's definition and hides it from the agent list.
AGENTS: list[BaseAgent] = [DeepResearchAgent(), DataAnalystAgent(), ExtractorAgent(), CustomAgent()]
BY_ID: dict[str, BaseAgent] = {a.id: a for a in AGENTS}
