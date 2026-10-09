"""Model prices for cost tracking, in USD per 1M tokens.

DEFAULT_PRICES are list prices at the time of writing and WILL drift — check
your provider's pricing page, and override or extend them with OBS_PRICING_JSON
(see ObservabilitySettings) rather than editing this file. Matching is by the
longest model-name prefix, so "gpt-4o-mini-2024-07-18" uses the "gpt-4o-mini"
entry. A model with no entry has no price: its cost is stored as NULL ("unknown")
instead of a made-up number. Local models (DEV/Ollama) cost 0 — see
backend/core/observability.py.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

# model prefix -> (input, cached input, output) per 1M tokens
DEFAULT_PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5": (1.25, 0.125, 10.00),
}


@lru_cache(maxsize=4)
def _prices(overrides_json: str | None) -> dict[str, tuple[float, float, float]]:
    prices = dict(DEFAULT_PRICES)
    if overrides_json:
        try:
            for model, p in json.loads(overrides_json).items():
                prices[model] = (float(p["input"]), float(p.get("cached_input", p["input"])), float(p["output"]))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("Ignoring invalid OBS_PRICING_JSON: %s", exc)
    return prices


def price_for(model: str, overrides_json: str | None = None) -> tuple[float, float, float] | None:
    """(input, cached input, output) USD per 1M tokens for `model`, or None."""
    prices = _prices(overrides_json)
    best = max((m for m in prices if model.startswith(m)), key=len, default=None)
    return prices[best] if best else None


def compute_cost(
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    overrides_json: str | None = None,
) -> float | None:
    """USD cost of one call, or None when the model has no known price.
    `input_tokens` includes the cached ones (as OpenAI's prompt_tokens does);
    cached tokens are billed at the cached rate, the rest at the input rate."""
    price = price_for(model, overrides_json)
    if price is None:
        return None
    rate_in, rate_cached, rate_out = price
    cached = min(max(cached_tokens, 0), input_tokens)
    return ((input_tokens - cached) * rate_in + cached * rate_cached + output_tokens * rate_out) / 1_000_000
