"""Transparent, async Ollama proxy.

Design principles (these are what keep Cline / native Ollama clients working):

  * The request is forwarded to the upstream Ollama server with its method,
    path, query string, headers and body preserved. We only *peek* the request
    JSON to read ``model`` (for the record) and never log prompt content.
  * The response is streamed back to the client **byte-for-byte, chunk by
    chunk, as it arrives** - we never rewrite, buffer, or reinterpret it.
    Ollama's ``thinking`` (reasoning) field, tool calls, and error bodies are
    all passed through untouched.
  * In parallel we keep a small rolling *tail* of the response bytes (bounded
    by ``TAIL_BUFFER_BYTES``) purely so we can JSON-parse the final NDJSON line
    (streaming) or the single object (non-streaming) and pull the usage stats
    Ollama already reports. This does not touch the bytes we forward.
  * Logging failures and client disconnects never crash the inference request.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

import httpx

from . import context
from .usage import build_record

# Headers that must not be forwarded to the upstream (request side).
_REQUEST_HEADER_DENY = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# Headers that must not be forwarded to the client (response side). We re-chunk
# the response, so content-length / transfer-encoding from upstream are invalid.
_RESPONSE_HEADER_DENY = {
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
}
_RESPONSE_PROXY_DENY = ("proxy-",)


def _filter_request_headers(scope: Dict[str, Any]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for k, v in scope.get("headers", []):
        name = k.decode("latin-1").lower()
        if name in _REQUEST_HEADER_DENY:
            continue
        out.append((name, v.decode("latin-1")))
    return out


def _filter_response_headers(headers: "httpx.Headers") -> List[Tuple[bytes, bytes]]:
    out: List[Tuple[bytes, bytes]] = []
    for name, value in headers.items():
        low = name.lower()
        if low in _RESPONSE_HEADER_DENY:
            continue
        if low.startswith(_RESPONSE_PROXY_DENY):
            continue
        out.append((low.encode("latin-1"), value.encode("latin-1")))
    return out


def tail_append(buf: bytearray, chunk: bytes, max_bytes: int) -> None:
    """Append a chunk to a bounded rolling buffer."""
    if not chunk or max_bytes <= 0:
        return
    buf.extend(chunk)
    overflow = len(buf) - max_bytes
    if overflow > 0:
        del buf[:overflow]


def _has_stats(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get("usage"), dict):
        return True  # OpenAI-compatible /v1 response or final stream chunk
    return any(k in obj for k in ("prompt_eval_count", "eval_count", "total_duration"))


def parse_tail_usage(tail: bytes) -> Optional[Dict[str, Any]]:
    """Extract the usage-bearing object from the bounded response tail.

    Handles:
      * streaming NDJSON -> the final ``done: true`` line carries the stats;
      * a single non-streaming JSON object carrying the stats at top level;
      * OpenAI-compatible SSE (``data: {...}`` lines) -> the chunk carrying
        ``usage`` (only sent when the client asked for
        ``stream_options.include_usage``).
    """
    if not tail:
        return None
    text = tail.decode("utf-8", "replace")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    candidates = list(reversed(lines))
    if text.strip():
        candidates.append(text)
    for cand in candidates:
        s = cand.strip()
        if s.startswith("data:"):
            s = s[5:].strip()
        if not s or s == "[DONE]":
            continue
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            continue
        if _has_stats(obj):
            return obj
    return None


async def _read_request_body(receive) -> Tuple[bytes, bool]:
    """Drain the incoming request body. Returns (body, client_disconnected)."""
    chunks: List[bytes] = []
    while True:
        msg = await receive()
        mtype = msg.get("type")
        if mtype == "http.request":
            if msg.get("body"):
                chunks.append(msg["body"])
            if not msg.get("more_body", False):
                return b"".join(chunks), False
        elif mtype == "http.disconnect":
            return b"".join(chunks), True
        else:
            return b"".join(chunks), False


def _peek_model_and_stream(body: bytes) -> Tuple[Optional[str], Optional[bool]]:
    """Read only ``model`` and ``stream`` from the request body (no logging)."""
    if not body:
        return None, None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    model = data.get("model")
    stream = data.get("stream")
    return (str(model) if model is not None else None), stream


async def _send_error(send, status: int, detail: str) -> None:
    body = json.dumps({"error": detail}).encode("utf-8")
    headers = [(b"content-type", b"application/json")]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _record(
    backend: str,
    model: Optional[str],
    endpoint: str,
    status: str,
    usage_payload: Optional[Dict[str, Any]],
    http_status: Optional[int],
) -> None:
    store = context.store
    if store is None:
        return
    try:
        record = build_record(
            backend=backend,
            model=model,
            endpoint=endpoint,
            status=status,
            usage_payload=usage_payload,
            http_status=http_status,
        )
        await store.append(record)
    except Exception:
        # Logging must never break inference.
        pass


async def forward(
    scope: Dict[str, Any],
    receive,
    send,
    *,
    record_usage: bool,
) -> None:
    """Generic streaming forward used by chat, generate, and passthrough."""
    cfg = context.config
    client: Optional["httpx.AsyncClient"] = context.client
    if cfg is None or client is None:
        await _send_error(send, 503, "logger not initialised")
        return

    path = scope.get("path", "/")
    qs = scope.get("query_string", b"")
    url = path + ("?" + qs.decode("latin-1") if qs else "")
    method = scope.get("method", "GET").upper()

    body, client_disconnected = await _read_request_body(receive)
    if client_disconnected:
        return

    model: Optional[str] = None
    if record_usage:
        model, _ = _peek_model_and_stream(body)

    upstream_headers = _filter_request_headers(scope)
    try:
        request = client.build_request(method, url, content=body or None, headers=upstream_headers)
        response = await client.send(request, stream=True)
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException:
        if record_usage:
            await _record(cfg.backend_name, model, path, "error", None, 504)
        await _send_error(send, 504, "upstream Ollama timed out")
        return
    except httpx.HTTPError:
        if record_usage:
            await _record(cfg.backend_name, model, path, "error", None, 502)
        await _send_error(send, 502, "upstream Ollama connection error")
        return
    except Exception:  # pragma: no cover - defensive
        if record_usage:
            await _record(cfg.backend_name, model, path, "error", None, 502)
        await _send_error(send, 502, "upstream Ollama error")
        return

    # ---- stream the response to the client, teeing a bounded tail ---------
    max_tail = cfg.tail_buffer_bytes
    tail = bytearray()
    status = "completed"
    cancelled = False
    http_status = response.status_code
    recorded = False
    try:
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": _filter_response_headers(response.headers),
            }
        )
        async for chunk in response.aiter_bytes():
            tail_append(tail, chunk, max_tail)
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        # Persist the record BEFORE the response terminator so the record is on
        # disk by the time the client considers the response complete.
        if record_usage:
            await _record(cfg.backend_name, model, path, status, parse_tail_usage(bytes(tail)), http_status)
            recorded = True
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    except asyncio.CancelledError:
        status = "cancelled"
        cancelled = True
    except Exception:
        # Client disconnect or upstream dropped mid-stream.
        status = "incomplete"
    finally:
        try:
            await response.aclose()
        except Exception:
            pass
        if record_usage and not recorded:
            await _record(cfg.backend_name, model, path, status, parse_tail_usage(bytes(tail)), http_status)

    if cancelled:
        raise asyncio.CancelledError()


# -- ASGI endpoints ----------------------------------------------------------
# Starlette wraps *plain functions* as request-style endpoints, so a raw ASGI
# handler must be a callable *instance*. ProxyHandler implements __call__(scope,
# receive, send) so Starlette treats it as an ASGI app.
class ProxyHandler:
    def __init__(self, record_usage: bool):
        self.record_usage = record_usage

    async def __call__(self, scope: Dict[str, Any], receive, send) -> None:
        await forward(scope, receive, send, record_usage=self.record_usage)


chat_handler = ProxyHandler(True)
generate_handler = ProxyHandler(True)
openai_handler = ProxyHandler(True)
passthrough_handler = ProxyHandler(False)
