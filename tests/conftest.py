"""Shared test fixtures.

Spins up two real uvicorn servers in threads:
  * a mock Ollama backend (deterministic, scriptable by model name)
  * the logger proxy pointed at that mock backend (fresh DATA_DIR per test)

This exercises the real async proxy path (not just unit functions), including
streaming, cancellation, and concurrency.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple

import pytest
import uvicorn

from app.config import Config
from app.main import create_app

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICING_FILE = os.path.join(REPO_ROOT, "pricing.json")


# ---------------------------------------------------------------------------
# uvicorn-in-thread helpers
# ---------------------------------------------------------------------------
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_uvicorn(app, host: str = "127.0.0.1", port: Optional[int] = None):
    port = port or _free_port()
    config = uvicorn.Config(app, host=host, port=port, log_level="error", loop="asyncio")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 25
    while time.time() < deadline:
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError("uvicorn server thread died during startup")
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn server failed to start in time")
    return port, server, thread


def stop_uvicorn(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Mock Ollama backend
# ---------------------------------------------------------------------------
async def _read_body(receive) -> bytes:
    chunks = []
    while True:
        msg = await receive()
        if msg["type"] == "http.request":
            if msg.get("body"):
                chunks.append(msg["body"])
            if not msg.get("more_body", False):
                return b"".join(chunks)
        elif msg["type"] == "http.disconnect":
            return b"".join(chunks)
        else:
            return b"".join(chunks)


_STATS = {
    "total_duration": 1_000_000_000,
    "load_duration": 1_000_000,
    "prompt_eval_count": 100,
    "prompt_eval_duration": 200_000_000,
    "eval_count": 50,
    "eval_duration": 400_000_000,
}


async def _send_json(send, status: int, obj: Dict[str, Any]) -> None:
    body = json.dumps(obj).encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _version(scope, receive, send) -> None:
    await _send_json(send, 200, {"version": "0.33.1"})


async def _tags(scope, receive, send) -> None:
    await _send_json(send, 200, {"models": [{"name": "test-model:latest", "size": 100}]})


def build_mock_ollama():
    from starlette.applications import Starlette
    from starlette.routing import Route

    class _ASGI:
        def __init__(self, fn):
            self.fn = fn

        async def __call__(self, scope, receive, send):
            await self.fn(scope, receive, send)

    async def _chat(scope, receive, send) -> None:
        data = json.loads((await _read_body(receive)) or b"{}")
        model = data.get("model", "test-model")
        stream = bool(data.get("stream", False))
        if model == "slow":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/x-ndjson")]})
            for i in range(25):
                await asyncio.sleep(0.15)
                line = json.dumps({"model": "slow", "message": {"content": str(i)}, "done": False}) + "\n"
                await send({"type": "http.response.body", "body": line.encode("utf-8"), "more_body": True})
            final = json.dumps({"model": "slow", "message": {"content": "done"}, "done": True, **_STATS}) + "\n"
            await send({"type": "http.response.body", "body": final.encode("utf-8"), "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        if model == "error":
            await _send_json(send, 500, {"error": "mock upstream failure"})
            return
        if model == "malformed":
            # Intentionally broken / incomplete body: the proxy must forward it
            # byte-for-byte and still record a (stats-less) completed request.
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/x-ndjson")]})
            await send({"type": "http.response.body", "body": b'{"model": "malformed", "partial": true\n', "more_body": True})
            await send({"type": "http.response.body", "body": b"CORRUPTED-TAIL", "more_body": False})
            return
        if model == "nostats":
            payload = {"model": model, "message": {"role": "assistant", "content": "hi", "thinking": "hmm"}, "done": True}
        else:
            payload = {
                "model": model,
                "created_at": "2026-01-01T00:00:00Z",
                "message": {"role": "assistant", "content": "Pong!", "thinking": "The user wants a short reply."},
                "done": True,
                "done_reason": "stop",
                **_STATS,
            }
            if data.get("tools") or model == "tool":
                payload["message"]["tool_calls"] = [
                    {"function": {"name": "get_weather", "arguments": {"city": "San Francisco"}}}
                ]
        if stream and model != "nostats":
            lines = [
                {"model": model, "message": {"role": "assistant", "content": "", "thinking": "The user"}, "done": False},
                {"model": model, "message": {"role": "assistant", "content": "Pong!", "thinking": ""}, "done": False},
            ]
            final = dict(payload, done=True)
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/x-ndjson")]})
            for ln in lines + [final]:
                await send({"type": "http.response.body", "body": (json.dumps(ln) + "\n").encode("utf-8"), "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await _send_json(send, 200, payload)

    async def _generate(scope, receive, send) -> None:
        data = json.loads((await _read_body(receive)) or b"{}")
        model = data.get("model", "test-model")
        stream = bool(data.get("stream", False))
        if model == "error":
            await _send_json(send, 500, {"error": "mock upstream failure"})
            return
        if model == "nostats":
            payload = {"model": model, "response": "Pong!", "thinking": "thinking", "done": True}
        else:
            payload = {"model": model, "response": "Pong!", "thinking": "The user wants a short reply.", "done": True, "done_reason": "stop", **_STATS}
        if stream:
            lines = [{"model": model, "response": "Pong!", "thinking": "The user", "done": False}, dict(payload, done=True)]
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/x-ndjson")]})
            for ln in lines:
                await send({"type": "http.response.body", "body": (json.dumps(ln) + "\n").encode("utf-8"), "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await _send_json(send, 200, payload)

    async def _slow(scope, receive, send) -> None:
        await _read_body(receive)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/x-ndjson")]})
        for i in range(10):
            await asyncio.sleep(0.2)
            line = json.dumps({"model": "slow", "message": {"content": str(i)}, "done": False}) + "\n"
            await send({"type": "http.response.body", "body": line.encode("utf-8"), "more_body": True})
        final = json.dumps({"model": "slow", "message": {"content": "done"}, "done": True, **_STATS}) + "\n"
        await send({"type": "http.response.body", "body": final.encode("utf-8"), "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    routes = [
        Route("/api/version", _ASGI(_version), methods=["GET"]),
        Route("/api/tags", _ASGI(_tags), methods=["GET"]),
        Route("/api/chat", _ASGI(_chat), methods=["POST"]),
        Route("/api/generate", _ASGI(_generate), methods=["POST"]),
        Route("/api/slow", _ASGI(_slow), methods=["POST"]),
    ]
    return Starlette(routes=routes)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def mock_ollama_url():
    app = build_mock_ollama()
    port, server, thread = start_uvicorn(app)
    base = f"http://127.0.0.1:{port}"
    yield base
    stop_uvicorn(server, thread)


def make_logger_config(backend_url: str, data_dir: str, **over) -> Config:
    base = dict(
        backend_name="rtx5090",
        backend_url=backend_url,
        listen_host="127.0.0.1",
        listen_port=0,
        data_dir=data_dir,
        log_max_bytes=0,
        log_backups=3,
        connect_timeout=5.0,
        read_timeout=30.0,
        total_timeout=30.0,
        max_connections=50,
        max_keepalive_connections=10,
        tail_buffer_bytes=2 * 1024 * 1024,
        pricing_file=PRICING_FILE,
    )
    base.update(over)
    return Config(**base)


@pytest.fixture
def logger_server(mock_ollama_url, tmp_path):
    data_dir = str(tmp_path / "data")
    cfg = make_logger_config(mock_ollama_url, data_dir)
    app = create_app(cfg)
    port, server, thread = start_uvicorn(app)
    base = f"http://127.0.0.1:{port}"
    yield {"base_url": base, "data_dir": data_dir}
    stop_uvicorn(server, thread)


@pytest.fixture
def dead_logger(tmp_path):
    data_dir = str(tmp_path / "data")
    cfg = make_logger_config("http://127.0.0.1:1", data_dir, connect_timeout=1.0, read_timeout=1.0)
    app = create_app(cfg)
    port, server, thread = start_uvicorn(app)
    base = f"http://127.0.0.1:{port}"
    yield {"base_url": base, "data_dir": data_dir}
    stop_uvicorn(server, thread)


def read_usage(data_dir: str):
    import glob

    records = []
    for path in [os.path.join(data_dir, "usage.jsonl")] + sorted(glob.glob(os.path.join(data_dir, "usage.jsonl.*"))):
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def wait_for_records(data_dir: str, min_count: int = 1, timeout: float = 6.0):
    """Poll for at least ``min_count`` records (a request may still be in flight)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        recs = read_usage(data_dir)
        if len(recs) >= min_count:
            return recs
        time.sleep(0.05)
    return read_usage(data_dir)
