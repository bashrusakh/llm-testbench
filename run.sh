#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
VENV_PATH="$PROJECT_ROOT/.venv"
REQ_FILE="$PROJECT_ROOT/python/requirements.txt"
SERVER_MODULE="python.server"
APP_URL="http://127.0.0.1:8765"

usage() {
  cat <<'EOF'
Usage: ./run.sh [--host HOST] [--port PORT] [--log-level LEVEL]

Creates .venv if needed, installs dependencies from python/requirements.txt,
then starts the LLM Testbench backend server.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 is required" >&2
  exit 1
fi

if [[ ! -f "$REQ_FILE" ]]; then
  echo "[ERROR] Missing requirements file: $REQ_FILE" >&2
  exit 1
fi

if [[ ! -d "$VENV_PATH" ]]; then
  python3 -m venv "$VENV_PATH"
fi

# shellcheck disable=SC1091
source "$VENV_PATH/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "$REQ_FILE"

export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$PROJECT_ROOT"
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$APP_URL" >/dev/null 2>&1 || true
fi
exec python -m "$SERVER_MODULE" "$@"
