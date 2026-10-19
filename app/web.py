"""HTTP dashboard for recorded Ollama token usage.

Two GET endpoints (registered before the Ollama pass-through catch-all):

  * ``/`` and ``/dashboard`` -> a self-contained HTML page (``static/index.html``)
  * ``/dashboard/data``      -> JSON: aggregated summary + a daily time series +
                                the most recent raw records

It reuses the exact same loading/aggregation/cost logic as ``report.py`` so the
numbers in the browser match the CLI. The page is served from this container's
port and exposes only aggregate token counts and cost *estimates* (no prompt or
response content is ever logged).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import context
from .pricing import cost_for_model, load_pricing

# ``report`` is a top-level module at the repository root (the same import the
# test suite already relies on). Make it importable regardless of the CWD.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import report as _report  # noqa: E402  (must follow the sys.path bootstrap)

_INDEX_PATH = os.path.join(_REPO_ROOT, "static", "index.html")
_index_cache: Optional[str] = None


def _load_index() -> str:
    global _index_cache
    if _index_cache is None:
        with open(_INDEX_PATH, "r", encoding="utf-8") as fh:
            _index_cache = fh.read()
    return _index_cache


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _day(ts: Optional[datetime]) -> Optional[str]:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _num(v: Any) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return 0.0


def build_series(records: List[Dict[str, Any]], pricing, cache_pct: float) -> List[Dict[str, Any]]:
    """Per-UTC-day buckets for the chart (tokens + cheapest/most-expensive cost)."""
    buckets: Dict[str, Dict[str, float]] = {}
    for r in records:
        day = _day(_parse_ts(r.get("timestamp")))
        if day is None:
            continue
        it = _num(r.get("input_tokens"))
        ot = _num(r.get("output_tokens"))
        tt = _num(r.get("total_tokens")) or (it + ot)
        b = buckets.setdefault(
            day,
            {"requests": 0, "input_tokens": 0.0, "output_tokens": 0.0, "total_tokens": 0.0,
             "cost_low": 0.0, "cost_high": 0.0},
        )
        b["requests"] += 1
        b["input_tokens"] += it
        b["output_tokens"] += ot
        b["total_tokens"] += tt
        if pricing and (it or ot):
            costs = [cost_for_model(mp, it, ot, cache_pct) for mp in pricing]
            b["cost_low"] += min(costs)
            b["cost_high"] += max(costs)
    out: List[Dict[str, Any]] = []
    for day in sorted(buckets):
        b = buckets[day]
        out.append({
            "day": day,
            "requests": int(b["requests"]),
            "input_tokens": int(b["input_tokens"]),
            "output_tokens": int(b["output_tokens"]),
            "total_tokens": int(b["total_tokens"]),
            "cost_low": round(b["cost_low"], 6),
            "cost_high": round(b["cost_high"], 6),
        })
    return out


def _collect(data_dir: str, pricing_file: str, period: str, cache_pct: float,
             now: datetime, recent_n: int = 200) -> Dict[str, Any]:
    """Blocking work: read the JSONL, aggregate, and shape the payload."""
    records = _report.load_usage(data_dir)
    pricing = load_pricing(pricing_file)
    cutoff = _report.parse_period(period, now)
    records = _report.filter_period(records, cutoff)
    summary = _report.aggregate(records, pricing, cache_pct, now, cutoff)
    series = build_series(records, pricing, cache_pct)
    recent = list(reversed(records))[:recent_n]
    return {
        "period": period,
        "cache_assumption_pct": cache_pct,
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "https://platform.openai.com/docs/pricing",
        "summary": summary,
        "series": series,
        "recent": recent,
    }


# -- ASGI endpoints ----------------------------------------------------------
def dashboard_index(request: Request) -> HTMLResponse:
    try:
        html = _load_index()
    except OSError:
        return HTMLResponse(
            "<h1>Dashboard assets not found</h1><p>Expected <code>static/index.html</code>.</p>",
            status_code=500,
        )
    return HTMLResponse(html)


async def dashboard_data(request: Request) -> JSONResponse:
    cfg = context.config
    if cfg is None:  # pragma: no cover - only if the lifespan hasn't run
        return JSONResponse({"error": "logger not initialised"}, status_code=503)

    period = (request.query_params.get("period") or "30d").strip() or "30d"
    raw_cache = request.query_params.get("cache", "0")
    try:
        cache_pct = float(raw_cache)
    except (TypeError, ValueError):
        cache_pct = 0.0

    now = datetime.now(timezone.utc)
    loop = asyncio.get_running_loop()
    try:
        payload = await loop.run_in_executor(
            None, _collect, cfg.data_dir, cfg.pricing_file, period, cache_pct, now
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(payload)