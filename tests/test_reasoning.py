import json

import httpx


def test_thinking_preserved_nonstreaming(logger_server):
    r = httpx.post(
        f"{logger_server['base_url']}/api/chat",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        timeout=15,
    )
    body = r.json()
    assert body["message"]["thinking"] == "The user wants a short reply."
    assert body["message"]["content"] == "Pong!"


def test_thinking_preserved_streaming(logger_server):
    with httpx.Client(timeout=30) as client:
        with client.stream(
            "POST", f"{logger_server['base_url']}/api/chat",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as r:
            raw = b"".join(r.iter_bytes())
    objs = [json.loads(ln) for ln in raw.decode("utf-8").split("\n") if ln.strip()]
    # At least one streamed chunk must carry a non-empty `thinking` field,
    # proving Qwen-style reasoning tokens are passed through untouched.
    thinking_chunks = [o for o in objs if o.get("message", {}).get("thinking")]
    assert thinking_chunks, "no reasoning chunks were forwarded"


def test_thinking_preserved_generate(logger_server):
    r = httpx.post(
        f"{logger_server['base_url']}/api/generate",
        json={"model": "test-model", "prompt": "hi", "stream": False},
        timeout=15,
    )
    assert r.json()["thinking"] == "The user wants a short reply."
