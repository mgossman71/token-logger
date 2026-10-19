import json
import os
from datetime import datetime, timezone

from conftest import PRICING_FILE
import report as report_mod


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _write_records(data_dir: str, n: int = 3, inp: int = 1000, out: int = 500) -> None:
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "usage.jsonl"), "w", encoding="utf-8") as f:
        for i in range(n):
            rec = {
                "request_id": f"r{i}",
                "timestamp": _now_iso(),
                "backend": "rtx5090",
                "model": "qwen3.8:27b",
                "endpoint": "/api/chat",
                "http_status": 200,
                "status": "completed",
                "input_tokens": inp,
                "output_tokens": out,
                "total_tokens": inp + out,
                "prompt_tokens_per_second": 100.0,
                "output_tokens_per_second": 50.0,
                "duration_seconds": 1.0,
                "load_duration_seconds": 0.001,
            }
            f.write(json.dumps(rec) + "\n")


def test_report_json_math(tmp_path):
    dd = str(tmp_path / "data")
    _write_records(dd, 3)
    out = tmp_path / "r.json"
    rc = report_mod.main(["--period", "all", "--format", "json", "--data-dir", dd, "--pricing", PRICING_FILE, "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["requests"] == 3
    assert data["tokens"]["input"] == 3000
    assert data["tokens"]["output"] == 1500
    assert data["tokens"]["total"] == 4500
    mini = [m for m in data["models"] if m["model"] == "gpt-4o-mini"][0]
    expected = 3 * (1000 * 0.15 + 500 * 0.60) / 1e6
    assert abs(mini["total_cost"] - expected) < 1e-9


def test_cache_assumption_reduces_cost(tmp_path):
    dd = str(tmp_path / "data")
    _write_records(dd, 1)

    def cost_for(cache: float) -> float:
        out = tmp_path / f"r{int(cache)}.json"
        report_mod.main(["--period", "all", "--format", "json", "--data-dir", dd, "--pricing", PRICING_FILE,
                         "--cache", str(cache), "--out", str(out)])
        d = json.loads(out.read_text())
        return [m for m in d["models"] if m["model"] == "gpt-4o-mini"][0]["total_cost"]

    c0 = cost_for(0)
    c75 = cost_for(75)
    assert c75 < c0, "cached input should be cheaper than uncached input"


def test_report_csv(tmp_path):
    dd = str(tmp_path / "data")
    _write_records(dd, 2)
    out = tmp_path / "r.csv"
    report_mod.main(["--period", "all", "--format", "csv", "--data-dir", dd, "--pricing", PRICING_FILE, "--out", str(out)])
    lines = out.read_text().strip().split("\n")
    assert "input_tokens" in lines[0]
    assert "cost_gpt-4o-mini" in lines[0]
    assert len(lines) == 3  # header + 2 data rows


def test_report_backend_filter(tmp_path):
    dd = str(tmp_path / "data")
    _write_records(dd, 2)
    out = tmp_path / "r.json"
    report_mod.main(["--period", "all", "--format", "json", "--data-dir", dd, "--pricing", PRICING_FILE,
                     "--backend", "nonexistent", "--out", str(out)])
    data = json.loads(out.read_text())
    assert data["requests"] == 0


def test_long_context_tier_applies(tmp_path):
    from app.pricing import cost_for_model, load_pricing

    tiered = [m for m in load_pricing(PRICING_FILE) if m.long_context_tiers][0]
    threshold = tiered.long_context_tiers[0]["min_tokens"]
    below = cost_for_model(tiered, threshold - 1, 100, 0)
    above = cost_for_model(tiered, threshold, 100, 0)
    assert above > below, "crossing the long-context threshold must raise the per-token rate"
