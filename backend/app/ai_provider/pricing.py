"""Model pricing — the single source of truth for token → USD conversion.

The agent-loop budget economy is denominated in **USD** (not tokens), because:
  * the Claude Agent SDK can only enforce a USD cap (``max_budget_usd``), not a
    token cap, on the leaf-worker CLI subprocess; and
  * a USD cap is stable across token-price changes — only this table moves.

Prices are **per 1,000,000 tokens**, AWS Bedrock, 2026 (matches the direct
Anthropic API in standard regions). We run the EU cross-region inference
profiles (``eu.anthropic.*``), which carry a ~10% surcharge, applied via
``_REGION_MULTIPLIER``.

Cache accounting (Anthropic prompt caching):
  * cache **read** input is billed at 0.1× the base input rate;
  * cache **write** (5-min TTL) is billed at 1.25× the base input rate.
These make caching the single biggest cost lever, so the economy must price
them correctly — a cache-heavy call should *look* cheap in telemetry.

Sources: AWS Bedrock pricing; platform.claude.com/docs/en/about-claude/pricing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# EU cross-region inference surcharge over the US/standard list price.
_REGION_MULTIPLIER = 1.10

# Cache price multipliers relative to the base *input* rate.
_CACHE_READ_MULTIPLIER = 0.10
_CACHE_WRITE_MULTIPLIER = 1.25


@dataclass(frozen=True)
class ModelPrice:
    """Per-1M-token USD prices (base, pre-region-surcharge)."""

    input_per_1m: float
    output_per_1m: float


# Keyed by a coarse tier token found in the model id. We match by substring
# (``haiku`` / ``sonnet`` / ``opus``) so dated ids like
# ``eu.anthropic.claude-haiku-4-5-20251001-v1:0`` resolve without an exact-id
# table that would rot on every model release.
_TIER_PRICES: Dict[str, ModelPrice] = {
    "haiku": ModelPrice(input_per_1m=1.0, output_per_1m=5.0),
    "sonnet": ModelPrice(input_per_1m=3.0, output_per_1m=15.0),
    "opus": ModelPrice(input_per_1m=5.0, output_per_1m=25.0),
}

# Fallback when a model id matches no known tier — price as Sonnet (mid/strong)
# so an unknown model is never under-priced (budget stays conservative).
_FALLBACK_TIER = "sonnet"


def _resolve_tier(model: str) -> str:
    """Map a model id/name to a pricing tier by substring match."""
    m = (model or "").lower()
    for tier in ("haiku", "sonnet", "opus"):
        if tier in m:
            return tier
    logger.warning("pricing: unknown model %r — pricing as %s", model, _FALLBACK_TIER)
    return _FALLBACK_TIER


def price_for(model: str) -> ModelPrice:
    """Return the (region-adjusted) per-1M-token price for a model id."""
    base = _TIER_PRICES[_resolve_tier(model)]
    return ModelPrice(
        input_per_1m=base.input_per_1m * _REGION_MULTIPLIER,
        output_per_1m=base.output_per_1m * _REGION_MULTIPLIER,
    )


def cost_usd(model: str, usage: Optional[Dict[str, Any]]) -> float:
    """Compute USD cost for one usage record.

    ``usage`` keys (from the SDK ``ResultMessage.usage`` / our budget_summary):
      input_tokens, output_tokens, cache_read_input_tokens,
      cache_creation_input_tokens. Cache-read tokens are billed at 0.1× input,
      cache-write at 1.25× input; both are SEPARATE from plain input_tokens
      (Anthropic reports them as distinct buckets), so we price each bucket and
      sum — we do NOT double-count cache tokens as plain input.

    Prefer the SDK's authoritative ``total_cost_usd`` when available (the caller
    passes usage without it here; this is the computed fallback).
    """
    if not usage:
        return 0.0
    p = price_for(model)

    def _get(*keys: str) -> int:
        for k in keys:
            v = usage.get(k)
            if v:
                return int(v)
        return 0

    in_tok = _get("input_tokens", "total_input_tokens")
    out_tok = _get("output_tokens", "total_output_tokens")
    cache_read = _get("cache_read_input_tokens", "cache_read_tokens")
    cache_write = _get("cache_creation_input_tokens", "cache_creation_tokens")

    cost = (
        in_tok * p.input_per_1m
        + cache_read * p.input_per_1m * _CACHE_READ_MULTIPLIER
        + cache_write * p.input_per_1m * _CACHE_WRITE_MULTIPLIER
        + out_tok * p.output_per_1m
    ) / 1_000_000.0
    return round(cost, 6)


def cost_from_budget_summary(model: str, budget_summary: Optional[Dict[str, Any]]) -> float:
    """USD cost from a worker ``budget_summary``.

    Uses the SDK's authoritative ``total_cost_usd`` if present (the leaf path
    gets it free from the CLI); otherwise computes from token buckets via
    :func:`cost_usd` (the in-house path has no cost field).
    """
    bs = budget_summary or {}
    actual = bs.get("total_cost_usd")
    if actual is not None:
        try:
            return float(actual)
        except (TypeError, ValueError):
            pass
    return cost_usd(model, bs)
