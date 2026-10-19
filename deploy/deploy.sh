#!/usr/bin/env bash
# =============================================================================
# Deploy the Ollama Token Logger on docker0 (target: /opt/ollama-token-logger)
#
#   ./deploy/deploy.sh                 # deploy logger + verify (safe default)
#   ./deploy/deploy.sh --wire-haproxy  # also attach HAProxy to the logger's
#                                      #   network and back up + show the
#                                      #   exact HAProxy change (no auto-edit)
#
# This script NEVER overwrites your HAProxy config. For --wire-haproxy it only
# (a) attaches HAProxy to the logger's Docker network and (b) prints the exact
# edit + a backup command. You apply the HAProxy edit yourself with vim.
# =============================================================================
set -euo pipefail

DEST="${DEST:-/opt/ollama-token-logger}"
SRC="${SRC:-$(cd "$(dirname "$0")/.." && pwd)}"
LOGGER_PORT="${LOGGER_PORT:-11435}"
BACKEND_URL="${OLLAMA_BACKEND_URL:-http://10.0.0.140:11434}"
BACKEND_NAME="${BACKEND_NAME:-rtx5090}"
NET="ollama-token-logger_net"
LOGGER_CTR="ollama-token-logger-5090"
HPROXY_CTR="${HPROXY_CONTAINER:-ollama-haproxy}"

log()  { printf '\033[1;32m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy]\033[0m %s\n' "$*"; }

log "Source:  $SRC"
log "Target:  $DEST"
log "Backend: $BACKEND_NAME -> $BACKEND_URL"

# ---------------------------------------------------------------- deploy
mkdir -p "$DEST"
rsync -a \
  --exclude .git --exclude .venv --exclude venv \
  --exclude data --exclude data-amd --exclude .env \
  --exclude __pycache__ --exclude .pytest_cache \
  "$SRC"/ "$DEST"/
cd "$DEST"

log "Creating .env from .env.example (if absent)"
if [ ! -f .env ]; then cp .env.example .env; fi
# Point this deployment at the real backend + name (idempotent).
sed -i.bak "s|^OLLAMA_BACKEND_URL=.*|OLLAMA_BACKEND_URL=$BACKEND_URL|" .env
sed -i.bak "s|^BACKEND_NAME=.*|BACKEND_NAME=$BACKEND_NAME|" .env
rm -f .env.bak

log "Building image and starting logger-5090"
docker compose build
docker compose up -d logger-5090

log "Waiting for the healthcheck to go healthy..."
healthy=""
for i in $(seq 1 40); do
  healthy="$(docker inspect --format '{{.State.Health.Status}}' "$LOGGER_CTR" 2>/dev/null || echo starting)"
  [ "$healthy" = "healthy" ] && break
  sleep 2
done
if [ "$healthy" != "healthy" ]; then
  warn "Health status is: $healthy  (not healthy yet)"
  docker logs --tail 40 "$LOGGER_CTR"
  exit 1
fi
log "Health: healthy"

# ---------------------------------------------------------------- verify
log "Verifying transparent passthrough to Ollama backend..."
ver="$(docker compose exec -T logger-5090 python -c "import os,urllib.request;print(urllib.request.urlopen('http://127.0.0.1:%s/api/version'%os.environ.get('LISTEN_PORT','11435'),timeout=5).read().decode())")"
log "  logger /api/version -> $ver"

# A tiny real inference request through the logger to confirm the full path.
log "Running a tiny inference request through the logger..."
docker compose exec -T logger-5090 python - <<'PY'
import os, json, urllib.request
port = os.environ.get("LISTEN_PORT", "11435")
body = json.dumps({
    "model": "qwen3.8:27b",
    "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
    "stream": False,
    "options": {"num_predict": 8},
}).encode()
req = urllib.request.Request(f"http://127.0.0.1:{port}/api/chat", data=body,
                             headers={"Content-Type": "application/json"})
r = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
print("  prompt_eval_count:", r.get("prompt_eval_count"),
      "eval_count:", r.get("eval_count"),
      "thinking:", bool(r.get("message", {}).get("thinking")))
PY

log "Showing the recorded usage..."
docker compose exec -T logger-5090 sh -c 'tail -n 3 /data/usage.jsonl' || true
log "Generating a cost report (last 30d)..."
docker compose exec -T logger-5090 python report.py --period 30d

# ---------------------------------------------------------------- haproxy
if [ "${1:-}" = "--wire-haproxy" ]; then
  echo
  log "Wiring HAProxy (network attach + backup; the config edit is yours to apply)"
  log "Attaching HAProxy to network '$NET' (enables service-name routing)..."
  docker network connect "$NET" "$HPROXY_CTR" || warn "network connect failed/already attached"

  cfg="$(docker inspect "$HPROXY_CTR" --format '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}{{"\n"}}{{end}}{{end}}' | grep -Ei 'haproxy.*\.cfg|haproxy.*\.conf|/etc/haproxy' | head -1 || true)"
  warn "Locate your HAProxy config (likely): ${cfg:-<see: docker inspect $HPROXY_CTR --format '{{json .Mounts}}' >}}"
  cat <<'MSG'

  Apply the HAProxy change manually (see deploy/haproxy-rtx5090.conf):
    1) BACKUP FIRST:  cp <haproxy.cfg> <haproxy.cfg>.bak-$(date +%Y%m%d-%H%M%S)
    2) vi <haproxy.cfg>
       replace:  server rtx5090 10.0.0.140:11434 check
       with:     server rtx5090-logger logger-5090:11435 check
    3) validate + reload:
       docker exec -it $HPROXY_CTR haproxy -c -f <mount-path-in-container>
       docker restart $HPROXY_CTR
    4) verify: curl -s http://docker0:4001/api/version
MSG
fi

log "Deployment complete."
log "  - Logger  : http://docker0:$LOGGER_PORT  (backend: $BACKEND_NAME)"
log "  - Reports : docker compose -f $DEST/docker-compose.yml exec logger-5090 python report.py --period 30d"
log "  - Logs    : docker compose -f $DEST/docker-compose.yml exec logger-5090 sh -c 'tail -f /data/usage.jsonl'"
