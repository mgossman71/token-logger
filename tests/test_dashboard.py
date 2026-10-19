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
