# Ollama Token Logger & Cloud Cost Estimator (OpenAI + Anthropic)

A transparent, asynchronous Ollama proxy that records real token usage per
request and estimates what the same workload would cost on cloud APIs (OpenAI + Anthropic).
Runs entirely through Docker Compose, sits behind your existing HAProxy, and
preserves Ollama's native API exactly — including streaming, Qwen `thinking`
reasoning, and tool calls.

```
Cline / other AI clients
        │
        ▼
   HAProxy (docker0:4001)          ← unchanged load balancer, leastconn
        │
        ▼
   Token Logger (port 11435)       ← this project: forwards + records usage
        │
        ▼
   Ollama server (10.0.0.140:11434)  ← RTX 5090, qwen3.8:27b
```

## What it does

- **Transparent Ollama proxy** for `/api/chat` and `/api/generate` (streaming
  and non-streaming), Ollama's OpenAI-compatible `/v1/chat/completions` and
  `/v1/completions`, and passthrough for every other Ollama endpoint
  (`/api/version`, `/api/tags`, `/api/show`, `/api/pull`, `/api/embed`, …).
- **Byte-perfect forwarding**: requests and responses are forwarded unchanged.
  It never converts Ollama to OpenAI format, never rewrites `thinking`
  reasoning, and passes tool calls through untouched.
- **Token accounting** from Ollama's own reported stats (`prompt_eval_count`,
  `eval_count`, `prompt_eval_duration`, `eval_duration`, `total_duration`,
  `load_duration`) — no invented numbers. Incomplete/cancelled/error requests
  are tracked separately. For `/v1` requests the counts come from the OpenAI
  `usage` object (`prompt_tokens`, `completion_tokens`, cached tokens); see
  [OpenAI-compatible clients](#openai-compatible-clients-v1).
- **File-based storage**: one JSON Lines record per inference request at
  `data/usage.jsonl`, with size-based rotation. Logging failures never break
  inference.
- **Cloud cost estimation (OpenAI + Anthropic)**: config-driven pricing + a CLI
  reporter with cache-hit assumptions, showing the estimated cost of the actual
  usage in the selected window (no projection).
- **Multi-backend ready**: one Compose instance per Ollama server. RTX 5090 is
  on by default; the AMD R9700 instance is a one-line, no-code-change addition.

## What it does **not** do

- No database, no Prometheus/Grafana, no GPU requirement.
- No prompt/response content, API keys, or other sensitive data is logged.
- No re-interpretation of model reasoning.



---

## Quick start (on docker0)

```bash
# 1. Copy this directory to docker0 (or clone it), then:
cd /opt/ollama-token-logger

# 2. Configure the backend
cp .env.example .env
#    (edit OLLAMA_BACKEND_URL / BACKEND_NAME if needed; defaults match the RTX 5090)

# 3. Build and start (RTX 5090 instance on port 11435)
docker compose build
docker compose up -d logger-5090

# 4. Confirm it is healthy and proxying to Ollama
docker compose ps
curl -s http://docker0:11435/api/version     # -> {"version":"0.33.1"}
```

Or run the bundled helper (safe by default; `--wire-haproxy` adds network
attach + a backed-up HAProxy change proposal):

```bash
./deploy/deploy.sh
./deploy/deploy.sh --wire-haproxy   # optional
```

## Configuration

All settings are environment variables (see `.env.example`):

| Variable                  | Default                    | Purpose                                   |
|---------------------------|----------------------------|-------------------------------------------|
| `BACKEND_NAME`            | `rtx5090`                  | Identity recorded in every usage record    |
| `OLLAMA_BACKEND_URL`      | `http://10.0.0.140:11434`  | Ollama server this instance proxies to     |
| `LISTEN_PORT`             | `11435`                    | Port the instance listens on               |
| `DATA_DIR`                | `/data`                    | Where `usage.jsonl` is written             |
| `LOG_MAX_BYTES`           | `104857600` (100 MB)       | Rotate when the log exceeds this (0=off)   |
| `LOG_BACKUPS`             | `5`                        | Number of rotated files to keep            |
| `CONNECT_TIMEOUT`         | `10`                       | Backend connect timeout (s)                |
| `READ_TIMEOUT`            | `1800`                     | Per-read timeout for long inference (s)    |
| `TAIL_BUFFER_BYTES`       | `8388608` (8 MiB)          | Rolling buffer used to read usage stats    |
| `MAX_CONNECTIONS`         | `200`                      | Upstream connection ceiling                |
| `TIMEZONE`                | `UTC`                      | IANA zone for "today", daily buckets and shown times (dashboard default; `report.py --tz` overrides) |

## Running the tests

Offline suite (mock Ollama backend, hermetic — always runs):

```bash
docker compose run --rm --no-deps logger-5090 python -m pytest -q
# ... or with a local venv:
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python -m pytest -q
```

Live integration suite (against the real RTX 5090 server; opt-in):

```bash
RUN_LIVE_TESTS=1 python -m pytest -q tests/test_integration_live.py
# Optionally override the target:
RUN_LIVE_TESTS=1 LIVE_BACKEND_URL=http://10.0.0.140:11434 LIVE_MODEL=qwen3.8:27b \
  python -m pytest -q tests/test_integration_live.py
```

## Reporting & cost estimates

```bash
# Text report (default)
docker compose exec logger-5090 python report.py --period today
docker compose exec logger-5090 python report.py --period 7d
docker compose exec logger-5090 python report.py --period 30d

# Cache-hit assumptions (default 0% = conservative)
docker compose exec logger-5090 python report.py --period 30d --cache 25
docker compose exec logger-5090 python report.py --period 30d --cache 50

# JSON / CSV export
docker compose exec logger-5090 python report.py --period 30d --format json --out /data/report.json
docker compose exec logger-5090 python report.py --period 30d --format csv  --out /data/report.csv

# Filters
docker compose exec logger-5090 python report.py --period 30d --backend rtx5090
docker compose exec logger-5090 python report.py --period 30d --models gpt-4o-mini,o1

# Timezone for "today" (default: $TIMEZONE, else UTC)
docker compose exec logger-5090 python report.py --period today --tz America/Chicago
```

Timestamps in `usage.jsonl` are always stored in UTC; `TIMEZONE` / `--tz` only
change where day boundaries fall and how times are displayed.

### OpenAI-compatible clients (`/v1`)

Clients pointed at Ollama's OpenAI-compatible API (e.g. Cline's "OpenAI
Compatible" provider, which lets you set reasoning effort) are recorded too.
Requests are still forwarded unchanged, so two limits apply:

- **Streaming needs `stream_options.include_usage: true`** from the client —
  only then does Ollama send a final chunk with `usage`. Without it the request
  is recorded as `completed` with null token counts. The logger never adds the
  option itself.
- **No timings**: the `usage` object has no durations, so tok/s and duration
  are null for `/v1` records.

### Inspecting the token log

```bash
docker compose exec logger-5090 sh -c 'tail -f /data/usage.jsonl'
docker compose exec logger-5090 sh -c 'wc -l /data/usage.jsonl'
docker compose exec logger-5090 sh -c 'python -c "import json,sys;[print(json.loads(l)[\"status\"],json.loads(l)[\"input_tokens\"],json.loads(l)[\"output_tokens\"]) for l in open(\"/data/usage.jsonl\")]"'
```

### A usage record

```json
{
  "request_id": "7840b1ec159646acab53c44b53b56b7b",
  "timestamp": "2026-10-19T02:26:36.419Z",
  "backend": "rtx5090",
  "model": "qwen3.8:27b",
  "endpoint": "/api/chat",
  "http_status": 200,
  "status": "completed",
  "input_tokens": 14,
  "output_tokens": 10,
  "total_tokens": 24,
  "prompt_tokens_per_second": 354.45,
  "output_tokens_per_second": 203.37,
  "duration_seconds": 0.203,
  "load_duration_seconds": 0.002
}
```

## Web dashboard

The logger also serves a small read-only, self-contained dashboard on the same
port — no extra services, no build step, works offline:

    http://<host>:11435/                                   # the page (also /dashboard)
    http://<host>:11435/dashboard/data?period=30d&cache=25 # JSON (for scripting)
    http://<host>:11435/dashboard/data?period=today&tz=America/Chicago

It shows the key usage blocks on top (requests, input / output / total tokens,
average speed), then what that usage would cost on each configured OpenAI and
Anthropic model (grouped by provider, cheapest highlighted), and the most recent
requests. "Today", the daily buckets and the shown times use the `TIMEZONE`
setting by default; the **Timezone** selector overrides it per viewer (remembered
in that browser). The numbers are computed with the exact
same logic as `report.py`. It exposes only aggregate token counts and cost
*estimates* — no prompt/response content is ever logged. Because it rides on the
proxy port it is reachable wherever the proxy is; set `DASHBOARD_ENABLED=0` to
turn it off and restore pure pass-through behaviour.

**Average speed** is token-weighted — total tokens ÷ total Ollama eval time —
not a mean of per-request rates, so a 1-token reply timed at ~1µs can't skew
it. The *input* speed counts the whole prompt, including the prefix Ollama
reuses from its KV cache, so on multi-turn chats it is far above raw prompt
processing speed. `/v1` records have no timings and are excluded. Costs under
$1 are shown to 4 decimals.

## Pricing configuration

Prices live in `pricing.json` (one file, no code). Each model entry supports a
`provider` (OpenAI or Anthropic), input / cached-input / output price per 1M
tokens, an effective date, and optional long-context tiers. **Verify these against
https://developers.openai.com/api/docs/pricing and https://platform.claude.com/docs/en/about-claude/pricing (rates last verified 2026-10-25) — they are a starting point, not gospel.**

Cost model per request, per model:

```
cost = ( input_tokens  * (1 - cache) * input_price
       + input_tokens  * cache       * cached_input_price
       + output_tokens * output_price ) / 1_000_000
```

**Long-context tiers** re-price the *whole* request when its input tokens are
strictly **above** the tier's `min_tokens` (OpenAI: >272K; Claude Haiku 5.5:
>100,000). A prompt of exactly the threshold is billed at base rates. The tier
is decided per request, never on period totals.

> **These are estimates, not billing predictions.** Ollama and OpenAI/Anthropic differ in
> tokenization, how reasoning tokens are counted, prompt-caching behaviour, and
> long-context pricing. Treat them as order-of-magnitude comparisons.


## Wiring HAProxy

The logger exposes Ollama's exact API on `11435`, so you only change the
backend **server** line — keep your `leastconn` balancing and the existing
`/api/version` health check. Full, copy-ready instructions are in
`deploy/haproxy-rtx5090.conf`.

**Preferred (service-name, no IP assumptions):**

```bash
# once — put HAProxy on the logger's network so Docker DNS resolves the service
docker network connect ollama-token-logger_net ollama-haproxy
```

```diff
# <your> HAProxy :4001 backend
-    server rtx5090 10.0.0.140:11434 check
+    server rtx5090-logger logger-5090:11435 check
```

**Fallback (published host address):** point the server at `docker0:<LAN-IP>:11435`
instead (see `deploy/haproxy-rtx5090.conf`). Always back up the HAProxy config
before editing and validate with `haproxy -c -f` before reloading.

Because the logger forwards `GET /api/version` to Ollama, HAProxy's existing
health check keeps working and now also verifies the logger→Ollama hop.

## Adding the AMD R9700 backend (later, no code changes)

```bash
AMD_OLLAMA_BACKEND_URL=http://<amd-ip>:11434 \
AMD_BACKEND_NAME=amd-r9700 \
docker compose --profile amd up -d
```

That starts `logger-amd` on `11436`, writing to `data-amd/usage.jsonl` and
tagging records `backend: amd-r9700`. Add its server line to HAProxy (see
`deploy/haproxy-add-amd.conf`) and keep your `leastconn` balancing.

## Project structure

```
app/            Starlette + httpx proxy (config, proxy, storage, usage, pricing)
  main.py       ASGI app factory + lifespan (client + store wiring)
  proxy.py      transparent forward, streaming, stat extraction, handlers
  storage.py    JSONL append + size-based rotation (async, thread-offloaded)
  usage.py      token/stat extraction from Ollama payloads (no invented data)
  pricing.py    cloud (OpenAI + Anthropic) pricing load + cost math
report.py       CLI reporter (text / JSON / CSV)
tests/          13 offline suites + 4 live integration tests (opt-in)
deploy/         deploy.sh + exact HAProxy change snippets
pricing.json    OpenAI + Anthropic price table (edit me)
```

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Health is not `healthy` | Logger process check failed: `docker compose logs logger-5090` |
| `502 upstream Ollama connection error` | Backend unreachable from the container: check `OLLAMA_BACKEND_URL` and that the container can route to `10.0.0.140`. Verify with `docker compose exec logger-5090 python -c "import urllib.request;print(urllib.request.urlopen('http://10.0.0.140:11434/api/version').read())"` |
| `504 upstream Ollama timed out` | Long inference: raise `READ_TIMEOUT` |
| No record for a request | Only `/api/chat`, `/api/generate` and `/v1/(chat/)completions` are recorded; other endpoints are passthrough by design. Check `docker compose logs logger-5090` for the path the client actually calls |
| `/v1` records have null tokens | The client streams without `stream_options.include_usage`; see [OpenAI-compatible clients](#openai-compatible-clients-v1) |
| "Today" starts at the wrong hour | Set `TIMEZONE` in `.env` (IANA name, e.g. `America/Chicago`) or pick a zone in the dashboard |
| Costs look off | Edit `pricing.json` and/or adjust `--cache`; these are estimates |

## What is verified here vs. on docker0

**Verified on this machine (Docker Desktop → real RTX 5090 @ 10.0.0.140):**
- Byte-identical passthrough of `/api/version` and `/api/tags`.
- Streaming + non-streaming `/api/chat` and `/api/generate` with Qwen `thinking`
  reasoning preserved and tool-call pass-through.
- Real token accounting (input/output/total, tok/s, duration) written to
  `data/usage.jsonl` and consumed by the reporter.
- Container healthcheck, in-container reporter (text/JSON/CSV), log rotation,
  cancellation safety, and 20-way concurrency — all covered by the test suite.

**Still to confirm on docker0:** the exact HAProxy server-line edit and the
`docker network connect` step (networking is environment-specific). The rest of
the stack is environment-independent and fully tested.

## License

Internal / MIT — see your org's policy.




## Requirements

- Docker + Docker Compose on `docker0`.
- The logger reaches the Ollama backend over the network (LAN IP or shared
  Docker network). No GPU.
