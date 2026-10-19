# Checkpoint — token-logger

_Last updated: 2026-10-18 · Owner: mgossman71_

**Repo:** https://github.com/mgossman71/token-logger (PRIVATE, branch `main`, local path `/Users/markgossman/Documents/GITHUB/token-logger`)

## One-line summary
A transparent, async Ollama proxy (Starlette/httpx) that records real per-request token usage to JSONL and estimates the equivalent OpenAI API cost. Packaged with Docker Compose, an offline + live test suite, and HAProxy deployment artifacts. **The core build is complete, fully tested (including live against the RTX 5090), and pushed to `main`.** What remains is the on-`docker0` HAProxy cutover (needs host access) plus optional follow-ups.

## Status
- ✅ **Done:** application, tests (offline + live), Docker packaging, docs, GitHub push.
- 🟡 **Remaining:** HAProxy cutover on `docker0` (manual step; requires host access the session cannot reach).
- ⬜ **Optional follow-ups:** add the AMD R9700 instance, audit pricing numbers, optional dashboard.
- ⛔ **Blocked:** none. (Only the HAProxy step is gated on `docker0` host access.)

## Completed
1. **Proxy app** (`app/`) — transparent Ollama proxy:
   - Byte-identical forwarding of `/api/chat` + `/api/generate` (streaming and non-streaming) and passthrough of every other Ollama endpoint (`/api/version`, `/api/tags`, `/api/show`, `/api/pull`, …).
   - **No OpenAI conversion** — Ollama-native API preserved; Qwen `thinking` reasoning and tool calls pass through unchanged.
   - Usage stats extracted from Ollama's own reported counts (bounded 8 MiB tail buffer) — never invented, never logs prompt content.
   - Record-before-terminator ordering; a logging failure never breaks inference; cancelled/incomplete/error requests are tracked with distinct `status` values.
   - JSONL storage + size-based rotation (after-append; active file stays ≤ `LOG_MAX_BYTES`, keeps `LOG_BACKUPS`).
   - Shared `httpx.AsyncClient` + lifespan; bounded upstream concurrency (`MAX_CONNECTIONS`).
2. **Cost CLI** (`report.py`) — `--period today|7d|30d|all`, `--cache N%`, `--format text|json|csv`, `--backend`, `--models`, `--out`, `--pricing`.
3. **Pricing** (`app/pricing.py`, `pricing.json`) — config-driven per-model input / cached-input / output price per 1M tokens + optional long-context tiers; cache-aware cost math.
4. **Tests** — 13 offline suites (mock Ollama, hermetic) + 4 live integration tests (opt-in). All green.
5. **Docker** — `Dockerfile` (python:3.12-slim, healthcheck, ships the test suite), `docker-compose.yml` (RTX 5090 instance + AMD instance behind `--profile amd`), `.dockerignore`, `.env` / `.env.example`.
6. **Deploy artifacts** — `deploy/deploy.sh`, `deploy/haproxy-rtx5090.conf`, `deploy/haproxy-add-amd.conf`.
7. **Docs** — `README.md` (architecture, quick start, config, tests, reports, HAProxy, AMD, troubleshooting, verified-vs-not).
8. **GitHub** — private repo created, 36 files pushed to `main` (commit `f5b10f6`); sensitive files excluded (`.env`, `data/`, `.venv/`, `*.jsonl`, `.vscode/`).

## Test results (all passing)
- **Offline (mock Ollama backend): 28/28** — passthrough, streaming, reasoning (`thinking`), tool calls, token accounting, backend-down (502 + recorded), client-cancel safety, 20-way concurrency, JSONL integrity, log rotation, cost/report math, CSV/JSON export.
- **Live (real RTX 5090 @ 10.0.0.140): 4/4** — byte-identical `/api/version` + `/api/tags`, real token accounting, Qwen streaming reasoning preserved, `/api/generate`.
- **In-container:** image builds, healthcheck reaches `healthy`, reporter runs inside the container (text/JSON/CSV).

## Verified facts (real RTX 5090)
- Ollama `0.33.1`; model `qwen3.8:27b`; sample record: 14 in / 10 out tokens, 354.45 in-tok/s, 203.37 out-tok/s, 0.203 s total.
- `data/usage.jsonl` record shape is stable and correctly consumed by `report.py`.

## Gotchas & decisions (so the next session does not redo this)
- **Starlette ASGI gotcha (hit & fixed):** plain-function route handlers are treated as `request`-style endpoints; a custom raw ASGI handler (`scope, receive, send`) MUST be a **callable class instance** (`ProxyHandler`), otherwise Starlette calls it with a `Request`.
- **No `curl` in `python:3.12-slim`:** the healthcheck uses a `python -c` HTTP probe. Don't swap in `curl` without installing it in the image.
- **`.venv`** is local-only (on the dev mac) for running the offline suite; tests also run inside the container.
- **`.gitignore`** excludes `.env`, `data/`, `*.jsonl`, `.venv/`, `__pycache__/`, `.vscode/`, `.idea/`. Do **not** commit real usage logs or the real `.env` (it has the backend URL). `.env.example` is the template that ships.
- **`.vscode/`** (`python.terminal.useEnvFile`) is intentionally not committed.

## How to run / test / deploy
```bash
# Offline tests (no GPU, hermetic)
. .venv/bin/activate && python -m pytest -q
#   …or in the image:
docker compose run --rm --no-deps logger-5090 python -m pytest -q

# Live tests (needs the real Ollama server; opt-in)
RUN_LIVE_TESTS=1 python -m pytest -q tests/test_integration_live.py

# Run locally and generate a report
docker compose up -d logger-5090
docker compose exec logger-5090 python report.py --period 30d --cache 25

# Full local deploy helper (verifies + optional HAProxy wiring)
./deploy/deploy.sh            # safe: build, up, health, one real request, report
./deploy/deploy.sh --wire-haproxy   # also attaches HAProxy to the network + prints the change
```

## What's left / next steps (in priority order)
1. **HAProxy cutover on `docker0`** — the only required step, and it needs host access.
   - Preferred (service-name, no IP assumptions):
     1. `docker network connect ollama-token-logger_net <haproxy-container>` (one time).
     2. In the HAProxy `:4001` backend, back up the config, then replace
        `server rtx5090 10.0.0.140:11434 check` → `server rtx5090-logger logger-5090:11435 check`.
     3. Validate (`haproxy -c -f …`) and reload; then `curl -s http://docker0:4001/api/version`.
   - Fallback: point the server at `docker0:<LAN-IP>:11435` (see `deploy/haproxy-rtx5090.conf`).
   - Keep existing `leastconn` balancing and the `/api/version` health check — the logger forwards that path to Ollama, so the check now also validates the logger→Ollama hop.
   - **Do this during a low-traffic window; the logger must be `healthy` before switching.**
2. **Confirm on `docker0`:** `docker compose ps` shows `logger-5090` healthy; `data/usage.jsonl` grows after real traffic; a `report.py` run shows sensible totals.
3. **(Optional) Add the AMD R9700 backend** later:
   `AMD_OLLAMA_BACKEND_URL=http://<amd-ip>:11434 AMD_BACKEND_NAME=amd-r9700 docker compose --profile amd up -d`
   then add its server line to HAProxy (see `deploy/haproxy-add-amd.conf`). Records will carry `backend: amd-r9700` and aggregate per-backend in reports.
4. **(Optional) Audit `pricing.json`** against https://openai.com/api/pricing/ — numbers are a documented starting point, not gospel.

## Open questions / assumptions to confirm
- **Costs are estimates, not billing predictions** — Ollama vs OpenAI differ in tokenization, how reasoning tokens are counted, prompt-caching behaviour, and long-context pricing. The per-1M figures in `pricing.json` should be double-checked.
- **`docker0` networking** — the exact `docker network connect` target / published IP and the HAProxy config location must be confirmed on the host (could not be inspected from this session).
- **AMD backend availability** — the R9700 was not yet installed; the AMD instance is pre-wired but untested against a real AMD Ollama server.

## Repo layout (36 files on `main`)
```
app/        __init__.py  config.py  context.py  main.py  pricing.py  proxy.py  storage.py  usage.py
tests/      conftest.py + 13 offline suites + test_integration_live.py
deploy/     deploy.sh  haproxy-rtx5090.conf  haproxy-add-amd.conf
report.py   app/healthcheck.py (in-image probe)   pricing.json
Dockerfile  docker-compose.yml  requirements.txt  .env.example  .dockerignore  .gitignore
README.md
```

## Quick "resume" checklist
- [ ] On `docker0`: `docker compose up -d logger-5090` → confirm `healthy`.
- [ ] Wire HAProxy (step 1 above) → `curl` through `:4001` → confirm `data/usage.jsonl` logs.
- [ ] `docker compose exec logger-5090 python report.py --period 30d` → sanity-check totals.
- [ ] (Optional) Add AMD instance + audit pricing.

