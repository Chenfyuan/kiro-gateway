# -*- coding: utf-8 -*-
"""
Model pricing configuration for cost estimation.

Prices in USD per 1 million tokens.
Can be overridden via MODEL_PRICING_FILE environment variable pointing to a JSON file.
"""

import json
import os
from typing import Optional

# Default pricing (USD per 1M tokens)
DEFAULT_PRICING = {
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5-20250514": {"input": 3.0, "output": 15.0},
    "amazon-nova-pro": {"input": 0.8, "output": 3.2},
    "amazon-nova-lite": {"input": 0.06, "output": 0.24},
    "amazon-nova-micro": {"input": 0.035, "output": 0.14},
}


def _load_pricing() -> dict:
    """Load pricing from file if configured, else use defaults."""
    pricing_file = os.getenv("MODEL_PRICING_FILE")
    if pricing_file and os.path.exists(pricing_file):
        try:
            with open(pricing_file, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_PRICING


MODEL_PRICING = _load_pricing()


def _lookup_pricing(model: str) -> Optional[dict]:
    """Return the rate entry for a model, or None when none is configured.

    Kept separate from get_cost so callers can tell "this model costs nothing"
    apart from "we have no rate for this model" - get_cost returns 0.0 for both,
    and a report that renders the second as $0.00 tells the reader the traffic
    was free. Every new model name (claude-opus-5, claude-haiku-4.5, ...) lands
    in that hole until DEFAULT_PRICING or MODEL_PRICING_FILE is updated.
    """
    pricing = MODEL_PRICING.get(model)
    if pricing:
        return pricing
    # Partial match, e.g. "claude-opus-4.6" falls back to the "claude-opus-4" rate
    for key, val in MODEL_PRICING.items():
        if key in model or model in key:
            return val
    return None


def has_pricing(model: str) -> bool:
    """Whether a rate is configured for this model (directly or by partial match)."""
    return _lookup_pricing(model) is not None


def get_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """
    Calculate cost in USD for given token usage.

    Args:
        model: Model name
        prompt_tokens: Number of input tokens
        completion_tokens: Number of output tokens

    Returns:
        Estimated cost in USD
    """
    pricing = _lookup_pricing(model)
    if not pricing:
        return 0.0

    input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]
    return input_cost + output_cost
