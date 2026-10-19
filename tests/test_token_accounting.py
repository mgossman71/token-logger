from conftest import read_usage


def test_token_accounting_math(logger_server):
    import httpx

    httpx.post(
        f"{logger_server['base_url']}/api/chat",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        timeout=15,
    )
    rec = read_usage(logger_server["data_dir"])[0]
    # _STATS: prompt 100 tok / 200ms ; output 50 tok / 400ms ; total 1000ms
    assert rec["input_tokens"] == 100
    assert rec["output_tokens"] == 50
    assert rec["total_tokens"] == 150
    assert abs(rec["prompt_tokens_per_second"] - 100 / 0.2) < 1e-6
    assert abs(rec["output_tokens_per_second"] - 50 / 0.4) < 1e-6
    assert abs(rec["duration_seconds"] - 1.0) < 1e-9
    assert abs(rec["load_duration_seconds"] - 0.001) < 1e-9


def test_missing_stats_not_invented(logger_server):
    import httpx

    r = httpx.post(
        f"{logger_server['base_url']}/api/chat",
        json={"model": "nostats", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        timeout=15,
    )
    assert r.status_code == 200
    rec = read_usage(logger_server["data_dir"])[-1]
    assert rec["status"] == "completed"
    assert rec["input_tokens"] is None
    assert rec["output_tokens"] is None
    assert rec["total_tokens"] is None
    assert rec["prompt_tokens_per_second"] is None
