import httpx


def test_dashboard_serves_html(logger_server):
    r = httpx.get(f"{logger_server['base_url']}/", timeout=20)
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "").lower()
    assert "Ollama Token Usage" in r.text

    # /dashboard is an alias for the same page
    r2 = httpx.get(f"{logger_server['base_url']}/dashboard", timeout=20)
    assert r2.status_code == 200
    assert r2.text == r.text


def test_dashboard_data_endpoint(logger_server):
    for i in range(3):
        httpx.post(
            f"{logger_server['base_url']}/api/chat",
            json={"model": "test-model", "messages": [{"role": "user", "content": f"x{i}"}], "stream": False},
            timeout=20,
        )

    r = httpx.get(f"{logger_server['base_url']}/dashboard/data?period=all&cache=0", timeout=20)
    assert r.status_code == 200
    data = r.json()

    s = data["summary"]
    assert s["requests"] >= 3
    assert s["tokens"]["input"] >= 0
    assert s["tokens"]["output"] >= 0
    assert isinstance(data["series"], list)
    assert data["recent"], "at least one recent record should be present"
    assert data["recent"][0]["request_id"]
    assert any(m["model"] for m in s["models"])
    providers = {m["provider"] for m in s["models"]}
    assert "openai" in providers and "anthropic" in providers

    # A bogus period is rejected with a 400.
    bad = httpx.get(f"{logger_server['base_url']}/dashboard/data?period=bogus", timeout=20)
    assert bad.status_code == 400


def test_dashboard_data_has_cache_fields(logger_server):
    base = logger_server["base_url"]
    for i in range(3):
        httpx.post(base + "/api/chat", json={"model": "test-model", "messages": [{"role": "user", "content": "x"}], "stream": False}, timeout=20)
    url = base + "/dashboard/data?period=all" + chr(38) + "cache=50"
    r = httpx.get(url, timeout=20)
    assert r.status_code == 200
    data = r.json()
    models = data["summary"]["models"]
    assert models, "expected at least one pricing model"
    for m in models:
        assert "base_cost" in m and "cached_cost" in m and "savings" in m and "measured_cost" in m
        assert m["base_cost"] >= m["cached_cost"]
        assert abs(m["savings"] - (m["base_cost"] - m["cached_cost"])) < 1e-5


def test_today_uses_viewer_timezone(tmp_path):
    """'today' starts at local midnight in the requested zone, not UTC midnight."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.web import _collect
    from conftest import PRICING_FILE

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # 02:00Z on Oct 25 is 21:00 Oct 24 in Chicago (CDT, UTC-5); 14:00Z is 09:00 local.
    lines = [
        '{"timestamp":"2026-10-25T02:00:00.000Z","status":"completed","input_tokens":10,"output_tokens":1,"total_tokens":11}',
        '{"timestamp":"2026-10-25T14:00:00.000Z","status":"completed","input_tokens":20,"output_tokens":2,"total_tokens":22}',
    ]
    (data_dir / "usage.jsonl").write_text("\n".join(lines) + "\n")

    chicago_now = datetime(2026, 10, 25, 15, 0, tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Chicago"))
    local = _collect(str(data_dir), PRICING_FILE, "today", 0.0, chicago_now)
    assert local["summary"]["requests"] == 1
    assert local["timezone"] == "America/Chicago"

    utc_now = chicago_now.astimezone(ZoneInfo("UTC"))
    assert _collect(str(data_dir), PRICING_FILE, "today", 0.0, utc_now)["summary"]["requests"] == 2

    # Daily buckets follow local days.
    days = [b["day"] for b in _collect(str(data_dir), PRICING_FILE, "all", 0.0, chicago_now)["series"]]
    assert days == ["2026-10-24", "2026-10-25"]


def test_dashboard_data_accepts_tz_param(logger_server):
    base = logger_server["base_url"]
    r = httpx.get(f"{base}/dashboard/data?period=today&tz=America/Chicago", timeout=20)
    assert r.status_code == 200 and r.json()["timezone"] == "America/Chicago"
    # Unknown zones fall back rather than erroring.
    r = httpx.get(f"{base}/dashboard/data?period=today&tz=Not/AZone", timeout=20)
    assert r.status_code == 200


def test_configured_timezone_is_default(mock_ollama_url, tmp_path):
    from app.main import create_app
    from conftest import make_logger_config, start_uvicorn, stop_uvicorn

    cfg = make_logger_config(mock_ollama_url, str(tmp_path / "data"), timezone="America/Chicago")
    port, server, thread = start_uvicorn(create_app(cfg))
    try:
        base = f"http://127.0.0.1:{port}"
        data = httpx.get(f"{base}/dashboard/data?period=today", timeout=20).json()
        assert data["timezone"] == "America/Chicago"
        assert data["default_timezone"] == "America/Chicago"
        # A viewer's explicit choice overrides the configured default.
        data = httpx.get(f"{base}/dashboard/data?period=today&tz=UTC", timeout=20).json()
        assert data["timezone"] == "UTC"
        assert data["default_timezone"] == "America/Chicago"
    finally:
        stop_uvicorn(server, thread)
