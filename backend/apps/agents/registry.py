"""The agents this service runs. To add one: write a BaseAgent subclass
(see base.py) and append an instance here."""
from __future__ import annotations

from backend.apps.agents.analyst import DataAnalystAgent
from backend.apps.agents.base import BaseAgent
from backend.apps.agents.research import DeepResearchAgent

AGENTS: list[BaseAgent] = [DeepResearchAgent(), DataAnalystAgent()]
BY_ID: dict[str, BaseAgent] = {a.id: a for a in AGENTS}
