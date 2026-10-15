#!/bin/zsh
set -e
APP_DIR="${0:A:h}"
RUNTIME_DIR="${APP_DIR:h}"
cd "$APP_DIR"
if [[ ! -x "$RUNTIME_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$RUNTIME_DIR/.venv"
  "$RUNTIME_DIR/.venv/bin/python" -m pip install -r requirements-platform.txt
fi
if curl --silent --fail http://127.0.0.1:8770/health >/dev/null 2>&1; then
  open http://127.0.0.1:8770/
  exit 0
fi
(open http://127.0.0.1:8770/ >/dev/null 2>&1) &
exec "$RUNTIME_DIR/.venv/bin/python" -m social_platform.server --port 8770
