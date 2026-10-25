"""Load OpenAI pricing and compute estimated cost for recorded Ollama usage.

Cost model (per model):
    input_tokens  -> billed at input_price, EXCEPT the fraction assumed to hit
                     the prompt cache, which is billed at cached_input_price.
    output_tokens -> billed at output_price.

    cost = ( input_tokens  * (1 - cache) * input_price
           + input_tokens  * cache      * cached_input_price
           + output_tokens * output_price ) / 1_000_000

Long-context tiers: if the request's total input tokens EXCEED a tier's
``min_tokens``, that tier's rates replace the base rates for the whole request
(OpenAI: "long context: >272K input tokens"; Anthropic Haiku 5.5: "prompts
over 100,000 tokens" - a prompt of exactly the threshold is billed at base).

These are ESTIMATES: Ollama and OpenAI tokenizer, reasoning-token accounting,
prompt caching behaviour, and context-dependent pricing all differ.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ModelPricing:
    model: str
    input_price: float
    cached_input_price: float
    output_price: float
    provider: str = ""
    effective_date: str = ""
    note: str = ""
    # list of {"min_tokens": int, "input_price": float,
    #          "cached_input_price": float, "output_price": float}
    long_context_tiers: List[Dict[str, float]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "input_price": self.input_price,
            "cached_input_price": self.cached_input_price,
            "output_price": self.output_price,
            "provider": self.provider,
            "effective_date": self.effective_date,
            "note": self.note,
            "long_context_tiers": self.long_context_tiers,
        }


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_pricing(path: str) -> List[ModelPricing]:
    """Load pricing entries from a JSON config file."""
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    models = data.get("models", data if isinstance(data, list) else [])
    out: List[ModelPricing] = []
    for m in models:
        tiers = []
        for t in m.get("long_context_tiers", []) or []:
            tiers.append(
                {
                    "min_tokens": int(t.get("min_tokens", 0)),
                    "input_price": _f(t.get("input_price")),
                    "cached_input_price": _f(t.get("cached_input_price")),
                    "output_price": _f(t.get("output_price")),
                }
            )
        out.append(
            ModelPricing(
                model=str(m.get("model", "")),
                input_price=_f(m.get("input_price")),
                cached_input_price=_f(m.get("cached_input_price")),
                output_price=_f(m.get("output_price")),
                provider=str(m.get("provider", "")),
                effective_date=str(m.get("effective_date", "")),
                note=str(m.get("note", "")),
                long_context_tiers=tiers,
            )
        )
    return out


def cost_for_model(
    mp: ModelPricing,
    input_tokens: float,
    output_tokens: float,
    cache_pct: float = 0.0,
) -> float:
    """Estimated USD cost for one request against this OpenAI model."""
    cache = max(0.0, min(100.0, float(cache_pct))) / 100.0
    in_tok = max(0.0, float(input_tokens))
    out_tok = max(0.0, float(output_tokens))

    inp = mp.input_price
    cip = mp.cached_input_price
    op = mp.output_price

    if mp.long_context_tiers:
        tiers = sorted(mp.long_context_tiers, key=lambda t: t.get("min_tokens", 0), reverse=True)
        for tier in tiers:
            if in_tok > tier.get("min_tokens", 0):
                inp = _f(tier.get("input_price"), inp)
                cip = _f(tier.get("cached_input_price"), cip)
                op = _f(tier.get("output_price"), op)
                break

    uncached_in = in_tok * (1.0 - cache)
    cached_in = in_tok * cache
    return (uncached_in * inp + cached_in * cip + out_tok * op) / 1_000_000.0


def cost_for_tokens(
    pricing: List[ModelPricing],
    input_tokens: float,
    output_tokens: float,
    cache_pct: float = 0.0,
) -> Dict[str, float]:
    """Map of model name -> estimated USD cost for a single request."""
    return {
        mp.model: cost_for_model(mp, input_tokens, output_tokens, cache_pct)
        for mp in pricing
    }


def cost_for_model_cached(
    mp: ModelPricing,
    input_tokens: float,
    output_tokens: float,
    cache_pct: float = 0.0,
    cached_input_tokens: Optional[float] = None,
) -> Dict[str, Any]:
    # base = no caching; cached = measured (cached_input_tokens) or assumed
    # (cache_pct) fraction of input billed at the cached-input price. Output
    # tokens are never discounted. Reuses cost_for_model (tier-aware).
    base = cost_for_model(mp, input_tokens, output_tokens, 0.0)
    if cached_input_tokens is not None:
        in_tok = max(0.0, float(input_tokens))
        cached_tok = max(0.0, min(float(cached_input_tokens), in_tok))
        frac = (cached_tok / in_tok) if in_tok > 0 else 0.0
        cached = cost_for_model(mp, input_tokens, output_tokens, frac * 100.0)
        source = "measured"
    else:
        cached = cost_for_model(mp, input_tokens, output_tokens, cache_pct)
        source = "assumed"
    return {
        "base": base,
        "cached": cached,
        "savings": base - cached,
        "source": source,
    }
