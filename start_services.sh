#!/usr/bin/env bash
# Start the three stage services as three separate processes.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p runs

pkill -f "services.publish_service:app" 2>/dev/null || true
pkill -f "services.spend_service:app"   2>/dev/null || true
pkill -f "services.bind_service:app"    2>/dev/null || true
sleep 0.3

PY=${PYTHON:-python3}
nohup $PY -m uvicorn services.publish_service:app --port 8101 --log-level warning \
  >runs/publish.log 2>&1 &
nohup $PY -m uvicorn services.spend_service:app   --port 8102 --log-level warning \
  >runs/spend.log 2>&1 &
nohup $PY -m uvicorn services.bind_service:app    --port 8103 --log-level warning \
  >runs/bind.log 2>&1 &
disown -a 2>/dev/null || true

for i in $(seq 1 40); do
  if curl -sf http://127.0.0.1:8101/healthz >/dev/null \
  && curl -sf http://127.0.0.1:8102/healthz >/dev/null \
  && curl -sf http://127.0.0.1:8103/healthz >/dev/null; then
    echo "publish :8101   spend :8102   bind :8103   -- all up"
    exit 0
  fi
  sleep 0.25
done
echo "services failed to come up" >&2
exit 1
