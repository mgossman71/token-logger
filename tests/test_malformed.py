import httpx

from conftest import read_usage


def test_malformed_response_forwarded_and_recorded(logger_server):
    r = httpx.post(
        f"{logger_server['base_url']}/api/chat",
        json={"model": "malformed", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        timeout=15,
    )
    # The broken body is forwarded unchanged (transparency) ...
    assert r.status_code == 200
    assert b"CORRUPTED-TAIL" in r.content
    # ... and no exception is raised; a stats-less record is still written.
    recs = read_usage(logger_server["data_dir"])
    assert recs, "a malformed response should still produce a record"
    assert recs[-1]["status"] == "completed"
    assert recs[-1]["input_tokens"] is None
    assert recs[-1]["output_tokens"] is None
