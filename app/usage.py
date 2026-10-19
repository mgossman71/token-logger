"""Token accounting extracted from Ollama's own reported usage statistics.

Ollama reports (in nanoseconds for durations) for both streaming (final chunk)
and non-streaming (single object) ``/api/chat`` and ``/api/generate``:

    prompt_eval_count      input tokens
    eval_count             output tokens
    prompt_eval_duration   ns spent processing the prompt
    eval_duration          ns spent generating the response
    total_duration         ns for the whole request
    load_duration          ns spent loading the model

We only ever surface numbers Ollama actually gave us. If a statistic is absent
we emit ``None`` (JSON null) rather than inventing a value.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _num(value: Any) -> Optional[float]:
    """Return value as a number, or None if it isn't a real number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _rate(tokens: Optional[float], duration_ns: Any) -> Optional[float]:
    if not tokens or duration_ns is None:
        return None
    duration_s = duration_ns / 1e9
    if duration_s <= 0:
        return None
    return tokens / duration_s


def extract_usage(payload: Optional[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """Extract usage metrics from a single Ollama chat/generate payload.

    ``payload`` should be the ``done: true`` chunk (streaming) or the single
    non-streaming response object.
    """
    p = payload or {}
    input_tokens = _num(p.get("prompt_eval_count"))
    output_tokens = _num(p.get("eval_count"))
    prompt_eval_duration = p.get("prompt_eval_duration")
    eval_duration = p.get("eval_duration")
    total_duration = p.get("total_duration")
    load_duration = p.get("load_duration")

    total_tokens = None
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    duration_seconds = None
    if total_duration is not None:
        try:
            duration_seconds = float(total_duration) / 1e9
        except (TypeError, ValueError):
            duration_seconds = None

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "prompt_tokens_per_second": _rate(input_tokens, prompt_eval_duration),
        "output_tokens_per_second": _rate(output_tokens, eval_duration),
        "duration_seconds": duration_seconds,
        "load_duration_seconds": (float(load_duration) / 1e9)
        if isinstance(load_duration, (int, float)) and not isinstance(load_duration, bool)
        else None,
    }


def _utc_iso(now: Optional[float] = None) -> str:
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    # Millisecond precision with a trailing Z, e.g. 2026-10-18T20:30:00.123Z
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def build_record(
    *,
    backend: str,
    model: Optional[str],
    endpoint: str,
    status: str,
    usage_payload: Optional[Dict[str, Any]],
    http_status: Optional[int] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Assemble a single usage.jsonl record.

    Sensitive data (prompt/response content, api keys) is never included.
    """
    rec: Dict[str, Any] = {
        "request_id": uuid.uuid4().hex,
        "timestamp": _utc_iso(now),
        "backend": backend,
        "model": model,
        "endpoint": endpoint,
        "http_status": http_status,
        "status": status,
    }
    rec.update(extract_usage(usage_payload))
    return rec
