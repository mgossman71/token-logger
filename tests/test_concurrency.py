import json
import threading

import httpx

from conftest import read_usage


def test_concurrent_requests(logger_server):
    N = 20
    errors = []
    lock = threading.Lock()

    def worker(i):
        try:
            stream = (i % 2 == 0)
            r = httpx.post(
                f"{logger_server['base_url']}/api/chat",
                json={"model": "test-model", "messages": [{"role": "user", "content": f"m{i}"}], "stream": stream},
                timeout=30,
            )
            if r.status_code != 200:
                with lock:
                    errors.append((i, r.status_code))
        except httpx.HTTPError as e:
            with lock:
                errors.append((i, repr(e)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"concurrent requests failed: {errors}"

    recs = read_usage(logger_server["data_dir"])
    assert len(recs) == N, f"expected {N} records, got {len(recs)}"
    # JSONL integrity: every line already parsed by read_usage (would raise otherwise).
    for rec in recs:
        assert rec["status"] == "completed"
        assert rec["input_tokens"] == 100 and rec["output_tokens"] == 50
    # All request_ids are unique.
    ids = {rec["request_id"] for rec in recs}
    assert len(ids) == N
