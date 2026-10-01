#!/usr/bin/env bash
# Inicia / para o servidor do Wi-Fi CSI Space Mapper.
#   ./run.sh            -> primeiro plano (Ctrl+C para sair)
#   ./run.sh --daemon   -> segundo plano (logs em logs/server.log)
#   ./run.sh --stop     -> encerra o servidor em segundo plano
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
PY="$VENV_DIR/bin/python"
PID_FILE="$ROOT_DIR/logs/server.pid"
LOG_FILE="$ROOT_DIR/logs/server.log"
mkdir -p "$ROOT_DIR/logs"

[[ -x "$PY" ]] || { echo "[erro] ambiente virtual ausente: rode ./install.sh primeiro" >&2; exit 1; }

set -a
[[ -f "$ROOT_DIR/.env" ]] && . "$ROOT_DIR/.env"
set +a
HOST="${WSM_HOST:-0.0.0.0}"
PORT="${WSM_PORT:-8080}"

case "${1:-}" in
  --stop)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      kill "$(cat "$PID_FILE")" && echo "servidor encerrado (pid $(cat "$PID_FILE"))"
      rm -f "$PID_FILE"
    else
      echo "nenhum servidor em execucao"
    fi
    ;;
  --daemon)
    cd "$ROOT_DIR"
    nohup "$PY" -m uvicorn server.app:app --host "$HOST" --port "$PORT" \
      >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    echo "servidor iniciado em http://$HOST:$PORT (pid $(cat "$PID_FILE"))"
    echo "logs: $LOG_FILE"
    ;;
  *)
    cd "$ROOT_DIR"
    echo "servidor em http://$HOST:$PORT  (Ctrl+C para sair)"
    exec "$PY" -m uvicorn server.app:app --host "$HOST" --port "$PORT"
    ;;
esac
