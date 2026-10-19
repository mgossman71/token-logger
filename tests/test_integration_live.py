"""Live integration tests against the real RTX 5090 Ollama server.

These run ONLY when `RUN_LIVE_TESTS=1` is set AND the backend is reachable,
so the default test suite stays fully offline / hermetic.

    RUN_LIVE_TESTS=1 python -m pytest -q tests/test_integration_live.py
"""
import json
import os
import socket

import httpx
import pytest

LIVE_BACKEND = os.environ.get("LIVE_BACKEND_URL", "http://10.0.0.140:11434")
MODEL = os.environ.get("LIVE_MODEL", "qwen3.8:27b")


def _host_port(url: str):
    host = url.split("//")[1].split(":")[0]
    port = int(url.split(":")[2].split("/")[0])
    return host, port


def _reachable() -> bool:
    try:
        h, p = _host_port(LIVE_BACKEND)
        with socket.create_connection((h, p), timeout=3):
            return True
    except OSError:
        return False


_live_enabled = os.environ.get("RUN_LIVE_TESTS") == "1"
live = pytest.mark.skipif(not (_live_enabled and _reachable()), reason="live Ollama backend not enabled/reachable")


@pytest.fixture
def live_logger(tmp_path):
    if not _live_enabled:
        pytest.skip("live tests disabled (set RUN_LIVE_TESTS=1)")
    from conftest import make_logger_config, start_uvicorn, stop_uvicorn
    from app.main import create_app

    data_dir = str(tmp_path / "data")
    cfg = make_logger_config(LIVE_BACKEND, data_dir, read_timeout=180, total_timeout=180)
    app = create_app(cfg)
    port, server, thread = start_uvicorn(app)
    base = f"http://127.0.0.1:{port}"
    yield {"base_url": base, "data_dir": data_dir, "direct": LIVE_BACKEND}
    stop_uvicorn(server, thread)


@live
def test_live_deterministic_passthrough_byte_identical(live_logger):
    """/api/version and /api/tags are deterministic -> proxied bytes must equal direct bytes."""
    for path in ("/api/version", "/api/tags"):
        direct = httpx.get(live_logger["direct"] + path, timeout=10)
        proxied = httpx.get(live_logger["base_url"] + path, timeout=10)
        assert direct.status_code == proxied.status_code == 200
        assert direct.content == proxied.content, "passthrough must be byte-for-byte identical"


@live
def test_live_nonstreaming_chat_records_usage(live_logger):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
        "stream": False,
        "options": {"num_predict": 8},
    }
    proxied = httpx.post(live_logger["base_url"] + "/api/chat", json=payload, timeout=180)
    assert proxied.status_code == 200
    body = proxied.json()
    assert body.get("prompt_eval_count") is not None
    assert body.get("eval_count") is not None
    assert "message" in body

    from conftest import read_usage

    recs = read_usage(live_logger["data_dir"])
    assert recs and recs[-1]["status"] == "completed"
    assert recs[-1]["input_tokens"] is not None
    assert recs[-1]["output_tokens"] is not None
    assert recs[-1]["output_tokens_per_second"] is not None


@live
def test_live_streaming_reasoning_preserved(live_logger):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "In one sentence, why is the sky blue?"}],
        "stream": True,
        "options": {"num_predict": 80},
    }
    with httpx.Client(timeout=180) as client:
        with client.stream("POST", live_logger["base_url"] + "/api/chat", json=payload) as r:
            raw = b"".join(r.iter_bytes())
    objs = [json.loads(ln) for ln in raw.decode("utf-8").split("\n") if ln.strip()]
    assert objs[-1]["done"] is True
    assert objs[-1].get("eval_count") is not None
    # Qwen3.8 emits reasoning; at least one chunk must carry a non-empty `thinking`.
    assert any(o.get("message", {}).get("thinking") for o in objs), "reasoning tokens were not preserved"

    from conftest import read_usage

    recs = read_usage(live_logger["data_dir"])
    assert recs and recs[-1]["status"] == "completed"


@live
def test_live_generate_endpoint(live_logger):
    payload = {"model": MODEL, "prompt": "Say pong", "stream": False, "options": {"num_predict": 5}}
    proxied = httpx.post(live_logger["base_url"] + "/api/generate", json=payload, timeout=180)
    assert proxied.status_code == 200
    body = proxied.json()
    assert body.get("prompt_eval_count") is not None
    assert body.get("eval_count") is not None
    from conftest import read_usage

    recs = read_usage(live_logger["data_dir"])
    assert any(r["endpoint"] == "/api/generate" for r in recs)
