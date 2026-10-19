import json
import os

import httpx


def test_every_line_is_valid_json(logger_server):
    for i in range(6):
        httpx.post(
            f"{logger_server['base_url']}/api/chat",
            json={"model": "test-model", "messages": [{"role": "user", "content": f"x{i}"}], "stream": (i % 2 == 0)},
            timeout=20,
        )
    path = os.path.join(logger_server["data_dir"], "usage.jsonl")
    assert os.path.exists(path)
    count = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            obj = json.loads(line)  # raises if the record is corrupt
            assert obj["request_id"]
            assert obj["timestamp"].endswith("Z")
            count += 1
    assert count == 6
