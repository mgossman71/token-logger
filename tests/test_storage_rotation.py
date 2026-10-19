import asyncio
import glob
import json
import os

from app.storage import JsonlStore


def test_rotation_caps_backups_and_keeps_valid_lines(tmp_path):
    path = str(tmp_path / "usage.jsonl")

    async def go():
        store = JsonlStore(path, max_bytes=200, backups=2)
        for i in range(60):
            await store.append({"i": i, "pad": "x" * 30})

    asyncio.run(go())

    backups = sorted(glob.glob(path + ".*"))
    assert os.path.exists(path), "active file should exist"
    assert os.path.getsize(path) < 200 + 60, "active file should stay near/under the threshold"
    assert len(backups) <= 2, f"backup count must be capped at 2, got {backups}"

    # Every line across all files must still be valid JSON, and retention must
    # have discarded the oldest records (backups cap total kept data).
    all_i = []
    for p in [path] + backups:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    all_i.append(json.loads(line)["i"])
    assert 0 < len(all_i) < 60, f"expected rotation to cap retention, kept {len(all_i)}"
    assert max(all_i) == 59, "the most recent record must be preserved"


def test_no_rotation_when_disabled(tmp_path):
    path = str(tmp_path / "usage.jsonl")

    async def go():
        store = JsonlStore(path, max_bytes=0, backups=5)
        for i in range(50):
            await store.append({"i": i, "pad": "y" * 40})

    asyncio.run(go())
    assert os.path.getsize(path) > 0
    assert glob.glob(path + ".*") == [], "no rotation should occur when max_bytes=0"
