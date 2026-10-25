#!/usr/bin/env python3
"""Command-line OpenAI cost reporter for recorded Ollama token usage.

Reads the JSONL usage log (usage.jsonl + rotated usage.jsonl.N) and reports:
  * request counts, token totals, average generation speed
  * backend utilisation
  * estimated cloud API cost (OpenAI + Anthropic) per configured model, for the
    actual usage in the selected window (no daily/monthly projection)

Examples:
    python report.py --period today
    python report.py --period 7d --cache 25
    python report.py --period 30d --format json --out report.json
    python report.py --period all --format csv --out cost.csv

These numbers are ESTIMATES (see README for caveats about tokenizer,
reasoning tokens, prompt caching, and long-context pricing differences).
"""
from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.pricing import ModelPricing, cost_for_model, load_pricing


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def load_usage(data_dir: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    files = [os.path.join(data_dir, "usage.jsonl")]
    files += sorted(glob.glob(os.path.join(data_dir, "usage.jsonl.*")))
    for path in files:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except (ValueError, TypeError):
                    continue
    return records


def parse_period(period: str, now: datetime) -> Optional[datetime]:
    """Return a cutoff datetime, or None for 'all'."""
    p = period.strip().lower()
    if p in ("all", "*"):
        return None
    if p == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    units = {"d": "days", "h": "hours", "w": "weeks"}
    if p[-1:] in units and p[:-1].isdigit():
        # Subtract in UTC: aware-datetime arithmetic in a local zone is
        # wall-clock based and would be off by an hour across a DST change.
        delta = timedelta(**{units[p[-1]]: int(p[:-1])})
        tz = now.tzinfo or timezone.utc
        return (now.astimezone(timezone.utc) - delta).astimezone(tz)
    raise ValueError(f"unrecognised period: {period!r}")


def period_days(cutoff: Optional[datetime], now: datetime, records: List[Dict[str, Any]]) -> float:
    if cutoff is None:
        if records:
            stamps = [ts for ts in (_parse_ts(r.get("timestamp", "")) for r in records) if ts]
            if len(stamps) >= 2:
                days = (max(stamps) - min(stamps)).total_seconds() / 86400.0
                return max(1.0, days)
        return 1.0
    days = (now - cutoff).total_seconds() / 86400.0
    return max(days, 1.0 / 24.0)


def filter_period(records: List[Dict[str, Any]], cutoff: Optional[datetime]) -> List[Dict[str, Any]]:
    if cutoff is None:
        return records
    out = []
    for r in records:
        ts = _parse_ts(r.get("timestamp", ""))
        # A record without a parseable timestamp can't be placed in a window;
        # keeping it would add its cost to every period. ("all" keeps it.)
        if ts is not None and ts >= cutoff:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _throughput(records: List[Dict[str, Any]], tokens_key: str, rate_key: str) -> Optional[float]:
    """Token-weighted tokens/second: sum(tokens) / sum(seconds).

    Each record's seconds are recovered as tokens / rate (rate was computed
    from Ollama's own duration). A plain mean of per-request rates lets a
    1-token request timed at ~1us (1,000,000 tok/s) swamp the average.
    """
    tokens = 0.0
    seconds = 0.0
    for r in records:
        t = _num(r.get(tokens_key))
        rate = _num(r.get(rate_key))
        if not t or not rate or rate <= 0:
            continue
        tokens += t
        seconds += t / rate
    if seconds <= 0:
        return None
    return tokens / seconds


def _record_total(r: Dict[str, Any]) -> float:
    # Fall back to input + output so the total always equals the shown parts.
    total = _num(r.get("total_tokens"))
    if total is not None:
        return total
    return (_num(r.get("input_tokens")) or 0) + (_num(r.get("output_tokens")) or 0)


def aggregate(
    records: List[Dict[str, Any]],
    pricing: List[ModelPricing],
    cache_pct: float,
    now: datetime,
    cutoff: Optional[datetime],
) -> Dict[str, Any]:
    total_input = sum(_num(r.get("input_tokens")) or 0 for r in records)
    total_output = sum(_num(r.get("output_tokens")) or 0 for r in records)
    total_tokens = sum(_record_total(r) for r in records)

    status_counts: Dict[str, int] = {}
    for r in records:
        s = r.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    avg_output_tps = _throughput(records, "output_tokens", "output_tokens_per_second")
    avg_prompt_tps = _throughput(records, "input_tokens", "prompt_tokens_per_second")

    backends: Dict[str, Dict[str, Any]] = {}
    for r in records:
        b = r.get("backend", "unknown")
        d = backends.setdefault(
            b, {"backend": b, "requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        )
        d["requests"] += 1
        d["input_tokens"] += _num(r.get("input_tokens")) or 0
        d["output_tokens"] += _num(r.get("output_tokens")) or 0
        d["total_tokens"] += _record_total(r)

    days = period_days(cutoff, now, records)
    models: List[Dict[str, Any]] = []
    for mp in pricing:
        base_cost = 0.0
        cached_cost = 0.0
        measured_cost = 0.0
        cached_input_tokens = 0.0
        input_tokens_total = 0.0
        for r in records:
            itok = _num(r.get("input_tokens"))
            otok = _num(r.get("output_tokens"))
            if itok is None and otok is None:
                continue
            itok = itok or 0.0
            otok = otok or 0.0
            input_tokens_total += itok
            base_cost += cost_for_model(mp, itok, otok, 0.0)
            cached_cost += cost_for_model(mp, itok, otok, cache_pct)
            cached_tok = _num(r.get("cached_input_tokens")) or 0.0
            cached_input_tokens += cached_tok
            frac = (cached_tok / itok) if itok > 0 else 0.0
            measured_cost += cost_for_model(mp, itok, otok, frac * 100.0)
        measured_cached_pct = round((cached_input_tokens / input_tokens_total * 100.0), 2) if input_tokens_total > 0 else 0.0
        models.append(
            {
                "model": mp.model,
                "provider": mp.provider,
                "input_price_per_m": mp.input_price,
                "cached_input_price_per_m": mp.cached_input_price,
                "output_price_per_m": mp.output_price,
                "effective_date": mp.effective_date,
                "base_cost": round(base_cost, 6),
                "cached_cost": round(cached_cost, 6),
                "savings": round(base_cost - cached_cost, 6),
                "measured_cost": round(measured_cost, 6),
                "cached_input_tokens": int(cached_input_tokens),
                "measured_cached_pct": measured_cached_pct,
                "total_cost": round(cached_cost, 6),
            }
        )

    return {
        "period": {
            "cutoff": cutoff.isoformat() if cutoff else None,
            "as_of": now.isoformat(),
            "approx_days": round(days, 4),
        },
        "cache_assumption_pct": cache_pct,
        "requests": len(records),
        "status_counts": status_counts,
        "tokens": {"input": total_input, "output": total_output, "total": total_tokens},
        "avg_prompt_tokens_per_second": avg_prompt_tps,
        "avg_output_tokens_per_second": avg_output_tps,
        "backends": list(backends.values()),
        "models": models,
        "note": (
            "Estimates only. Ollama vs OpenAI/Anthropic differ in tokenizer, "
            "reasoning-token accounting, prompt caching and long-context pricing."
        ),
    }


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------
def _fmt_int(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{int(round(v)):,}"


def _fmt_float(v: Optional[float], nd: int = 2) -> str:
    return "n/a" if v is None else f"{v:,.{nd}f}"


def format_text(summary: Dict[str, Any]) -> str:
    L: List[str] = []
    tok = summary["tokens"]
    L.append("=" * 62)
    L.append("Ollama Token Usage  ->  Estimated cloud API cost (OpenAI + Anthropic)")
    L.append("=" * 62)
    cov = summary["period"]
    L.append(
        f"Window        : {cov['cutoff'] or 'beginning of log'}  ->  {cov['as_of']}  (~{cov['approx_days']}d)"
    )
    L.append(f"Cache assume  : {summary['cache_assumption_pct']:.0f}% of input billed at cached rate")
    L.append("")
    L.append(f"Requests      : {summary['requests']:,}   {summary['status_counts']}")
    L.append(f"Input tokens  : {_fmt_int(tok['input'])}")
    L.append(f"Output tokens : {_fmt_int(tok['output'])}")
    L.append(f"Total tokens  : {_fmt_int(tok['total'])}")
    L.append(f"Avg in  speed : {_fmt_float(summary['avg_prompt_tokens_per_second'])} tok/s")
    L.append(f"Avg out speed : {_fmt_float(summary['avg_output_tokens_per_second'])} tok/s")
    L.append("")
    L.append("Backend utilisation (by request count)")
    L.append("-" * 62)
    for b in summary["backends"]:
        L.append(
            f"  {b['requests']:>6} req  in={_fmt_int(b['input_tokens'])}  "
            f"out={_fmt_int(b['output_tokens'])}  total={_fmt_int(b['total_tokens'])}   {b['backend']}"
        )
    L.append("")
    L.append("Estimated cloud API cost for this usage (OpenAI + Anthropic) - no projection")
    L.append("-" * 74)
    L.append(f"  {'model':<22} {'provider':<11} {'in $/M':>9} {'out $/M':>9} {'cost (window)':>14}")
    _prov_order = {"openai": 0, "anthropic": 1}
    for m in sorted(
        summary["models"],
        key=lambda x: (_prov_order.get(x.get("provider", ""), 99), x["total_cost"]),
    ):
        L.append(
            f"  {m['model']:<22} {m.get('provider', ''):<11} {m['input_price_per_m']:>9.3f} "
            f"{m['output_price_per_m']:>9.3f} ${m['total_cost']:>13,.2f}"
        )
    L.append("")
    L.append("Note: " + summary["note"])
    return "\n".join(L)


def format_csv(records: List[Dict[str, Any]], pricing: List[ModelPricing], cache_pct: float) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    header = [
        "request_id",
        "timestamp",
        "backend",
        "model",
        "endpoint",
        "status",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens_per_second",
        "output_tokens_per_second",
        "duration_seconds",
    ] + [f"cost_{mp.model}" for mp in pricing]
    writer.writerow(header)
    for r in records:
        itok = _num(r.get("input_tokens")) or 0.0
        otok = _num(r.get("output_tokens")) or 0.0
        row = [
            r.get("request_id"),
            r.get("timestamp"),
            r.get("backend"),
            r.get("model"),
            r.get("endpoint"),
            r.get("status"),
            r.get("input_tokens"),
            r.get("output_tokens"),
            r.get("total_tokens"),
            r.get("prompt_tokens_per_second"),
            r.get("output_tokens_per_second"),
            r.get("duration_seconds"),
        ] + [round(cost_for_model(mp, itok, otok, cache_pct), 8) for mp in pricing]
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Estimated OpenAI cost from Ollama usage.")
    p.add_argument("--period", default="30d", help="today | 1d | 7d | 30d | 2w | 12h | all (default 30d)")
    p.add_argument("--cache", type=float, default=0.0, help="cache-hit %% assumption for input (default 0)")
    p.add_argument("--format", choices=["text", "json", "csv"], default="text")
    p.add_argument("--out", help="write report to this file instead of stdout")
    p.add_argument("--pricing", default=os.environ.get("PRICING_FILE") or "./pricing.json")
    p.add_argument("--data-dir", default=os.environ.get("DATA_DIR") or "./data")
    p.add_argument("--backend", help="filter to a single backend name")
    p.add_argument("--models", help="comma-separated subset of OpenAI model names")
    p.add_argument("--tz", default=os.environ.get("TIMEZONE") or "UTC",
                   help="IANA timezone for 'today' (e.g. America/Chicago; default $TIMEZONE or UTC)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        now = datetime.now(ZoneInfo(args.tz))
    except (ZoneInfoNotFoundError, ValueError):
        print(f"unknown timezone: {args.tz!r}", file=sys.stderr)
        return 2
    try:
        cutoff = parse_period(args.period, now)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    records = load_usage(args.data_dir)
    if args.backend:
        records = [r for r in records if r.get("backend") == args.backend]
    records = filter_period(records, cutoff)

    pricing = load_pricing(args.pricing)
    if args.models:
        wanted = {m.strip() for m in args.models.split(",") if m.strip()}
        pricing = [mp for mp in pricing if mp.model in wanted]

    summary = aggregate(records, pricing, args.cache, now, cutoff)

    if args.format == "text":
        out = format_text(summary)
    elif args.format == "json":
        out = json.dumps(summary, indent=2)
    else:
        out = format_csv(records, pricing, args.cache)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"Wrote report to {args.out}")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
