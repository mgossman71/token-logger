import httpx

from conftest import read_usage


def test_backend_down_returns_502_and_records_error(dead_logger):
    r = httpx.post(
        f"{dead_logger['base_url']}/api/chat",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        timeout=15,
    )
    assert r.status_code == 502
    assert "error" in r.json()
    recs = read_usage(dead_logger["data_dir"])
    assert recs, "a failed inference request should still be recorded"
    assert recs[-1]["status"] == "error"
    assert recs[-1]["input_tokens"] is None


def test_upstream_error_status_preserved(logger_server):
    r = httpx.post(
        f"{logger_server['base_url']}/api/chat",
        json={"model": "error", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        timeout=15,
    )
    # The proxy forwards Ollama's own error status code unchanged.
    assert r.status_code == 500
    assert "error" in r.json()
    recs = read_usage(logger_server["data_dir"])
    assert recs[-1]["http_status"] == 500
