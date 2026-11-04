#!/usr/bin/env bash
# Démarre l'API et le worker ensemble. Ctrl+C arrête les deux.
# Variables utiles : PORT (défaut 8000), GATEWAY_FAKE_NOW (heure simulée, ex. 2026-11-04T23:30).
set -euo pipefail
cd "$(dirname "$0")"

[ -x .venv/bin/python ] || ./setup.sh
PY=.venv/bin/python
PORT="${PORT:-8000}"
export GATEWAY_BASE_URL="${GATEWAY_BASE_URL:-http://127.0.0.1:$PORT}"

"$PY" manage.py migrate --verbosity 0

"$PY" manage.py runserver "127.0.0.1:$PORT" --noreload &
SERVER_PID=$!
"$PY" manage.py worker &
WORKER_PID=$!

stop() {
  trap - INT TERM EXIT
  kill "$SERVER_PID" "$WORKER_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap stop INT TERM EXIT

echo "API     : http://127.0.0.1:$PORT/api/docs"
[ -n "${GATEWAY_FAKE_NOW:-}" ] && echo "Heure simulée : $GATEWAY_FAKE_NOW (heure du Bénin)"
echo "Ctrl+C pour arrêter."

# Stop everything as soon as one of the two processes exits.
wait -n
