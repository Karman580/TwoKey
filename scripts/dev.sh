#!/usr/bin/env bash
# Start the dev ledger and the TwoKey services, then open http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")/.."

[ -x .venv/bin/python ] || { echo "run 'make setup' first"; exit 1; }
[ -f .env ] || .venv/bin/python scripts/gen_keys.py
set -a; . ./.env; set +a

mkdir -p .bin .data
(cd chaincode && go build -o ../.bin/devledger ./cmd/devledger)
.bin/devledger &
LEDGER_PID=$!
trap 'kill $LEDGER_PID 2>/dev/null' EXIT

for _ in $(seq 1 50); do
  curl -sf "$LEDGER_URL/health" >/dev/null && break
  sleep 0.1
done
curl -sf "$LEDGER_URL/health" >/dev/null || { echo "devledger did not start"; exit 1; }

echo "TwoKey dashboard: http://${APP_HOST}:${APP_PORT}"
cd services && ../.venv/bin/python -m twokey.app
