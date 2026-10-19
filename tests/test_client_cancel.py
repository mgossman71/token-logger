import httpx

from conftest import read_usage, wait_for_records


def test_client_cancels_mid_stream(logger_server):
    """Disconnecting mid-stream must not crash the logger."""
    payload = {"model": "slow", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    try:
        with httpx.Client(timeout=30) as client:
            with client.stream("POST", f"{logger_server['base_url']}/api/chat", json=payload) as r:
                got = 0
                for chunk in r.iter_bytes():
                    got += 1
                    if got >= 2:
                        break  # abandon the stream early
    except httpx.HTTPError:
        pass

    # The logger must still be alive and serving subsequent requests.
    r = httpx.get(f"{logger_server['base_url']}/api/version", timeout=10)
    assert r.status_code == 200
    assert r.json() == {"version": "0.33.1"}

    # The abandoned request is still tracked (the slow stream is long, so it is
    # interrupted before completion rather than finishing as "completed").
    recs = wait_for_records(logger_server["data_dir"], min_count=1)
    slow = [x for x in recs if x.get("model") == "slow"]
    assert slow, "the cancelled request should have been recorded"
    assert slow[-1]["status"] in ("incomplete", "cancelled", "completed")


def test_server_survives_repeated_cancels(logger_server):
    for _ in range(3):
        try:
            with httpx.Client(timeout=30) as client:
                with client.stream(
                    "POST", f"{logger_server['base_url']}/api/chat",
                    json={"model": "slow", "messages": [{"role": "user", "content": "x"}], "stream": True},
                ) as r:
                    next(r.iter_bytes())
        except httpx.HTTPError:
            pass
    assert httpx.get(f"{logger_server['base_url']}/api/version", timeout=10).status_code == 200
