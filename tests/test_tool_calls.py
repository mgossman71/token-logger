import json

import httpx


def test_tool_calls_nonstreaming(logger_server):
    r = httpx.post(
        f"{logger_server['base_url']}/api/chat",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "weather in SF?"}],
            "stream": False,
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
        },
        timeout=15,
    )
    body = r.json()
    assert body["message"]["tool_calls"] == [
        {"function": {"name": "get_weather", "arguments": {"city": "San Francisco"}}}
    ]


def test_tool_calls_streaming(logger_server):
    with httpx.Client(timeout=30) as client:
        with client.stream(
            "POST", f"{logger_server['base_url']}/api/chat",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "weather?"}],
                "stream": True,
                "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
            },
        ) as r:
            raw = b"".join(r.iter_bytes())
    objs = [json.loads(ln) for ln in raw.decode("utf-8").split("\n") if ln.strip()]
    final = objs[-1]
    assert final["message"]["tool_calls"], "tool_calls missing from final streamed chunk"
    assert final["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
