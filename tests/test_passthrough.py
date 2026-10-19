import httpx

from conftest import read_usage


def test_version_passthrough(logger_server):
    r = httpx.get(f"{logger_server['base_url']}/api/version", timeout=10)
    assert r.status_code == 200
    assert r.json() == {"version": "0.33.1"}
    # /api/version is not an inference endpoint: no usage record.
    assert read_usage(logger_server["data_dir"]) == []


def test_tags_passthrough(logger_server):
    r = httpx.get(f"{logger_server['base_url']}/api/tags", timeout=10)
    assert r.status_code == 200
    assert "models" in r.json()


def test_chat_nonstreaming(logger_server):
    r = httpx.post(
        f"{logger_server['base_url']}/api/chat",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["message"]["content"] == "Pong!"
    assert body["done"] is True

    recs = read_usage(logger_server["data_dir"])
    assert len(recs) == 1
    rec = recs[0]
    assert rec["status"] == "completed"
    assert rec["input_tokens"] == 100
    assert rec["output_tokens"] == 50
    assert rec["total_tokens"] == 150
    assert rec["endpoint"] == "/api/chat"
    assert rec["model"] == "test-model"
    assert rec["backend"] == "rtx5090"
    assert "messages" not in str(rec)  # never log prompt content
    assert rec["request_id"] and rec["timestamp"].endswith("Z")


def test_generate_nonstreaming(logger_server):
    r = httpx.post(
        f"{logger_server['base_url']}/api/generate",
        json={"model": "test-model", "prompt": "hi", "stream": False},
        timeout=15,
    )
    assert r.status_code == 200
    assert r.json()["response"] == "Pong!"
    recs = read_usage(logger_server["data_dir"])
    assert any(rec["endpoint"] == "/api/generate" and rec["status"] == "completed" for rec in recs)
