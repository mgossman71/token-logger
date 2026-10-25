"""OpenAI-compatible /v1/chat/completions: forwarded unchanged and recorded."""
import json

import httpx

from conftest import read_usage


def _post(base_url, **payload):
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
    body.update(payload)
    with httpx.Client(timeout=30) as client:
        with client.stream("POST", f"{base_url}/v1/chat/completions", json=body) as r:
            assert r.status_code == 200
            return b"".join(r.iter_bytes())


def test_non_streaming_recorded(logger_server):
    raw = _post(logger_server["base_url"], stream=False)
    assert json.loads(raw)["usage"]["prompt_tokens"] == 120
    recs = read_usage(logger_server["data_dir"])
    assert len(recs) == 1
    rec = recs[0]
    assert rec["endpoint"] == "/v1/chat/completions"
    assert rec["status"] == "completed"
    assert rec["model"] == "test-model"
    assert (rec["input_tokens"], rec["output_tokens"], rec["total_tokens"]) == (120, 30, 150)
    assert rec["duration_seconds"] is None


def test_streaming_with_include_usage_recorded(logger_server):
    raw = _post(logger_server["base_url"], stream=True, stream_options={"include_usage": True})
    text = raw.decode("utf-8")
    # SSE framing passes through untouched.
    assert text.rstrip().endswith("data: [DONE]")
    assert '"reasoning": "hmm"' in text
    recs = read_usage(logger_server["data_dir"])
    assert len(recs) == 1
    assert (recs[0]["input_tokens"], recs[0]["output_tokens"]) == (120, 30)


def test_streaming_without_usage_still_recorded_with_null_tokens(logger_server):
    _post(logger_server["base_url"], stream=True)
    recs = read_usage(logger_server["data_dir"])
    assert len(recs) == 1
    assert recs[0]["status"] == "completed"
    assert recs[0]["input_tokens"] is None
