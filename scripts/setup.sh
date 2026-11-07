#!/usr/bin/env bash
# One-time setup: Python virtualenv for the backend, npm packages for the frontend, .env.
set -euo pipefail
cd "$(dirname "$0")/.."

pick_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}

echo "==> Backend"
if command -v uv >/dev/null 2>&1; then
  (cd backend && uv venv --quiet .venv --python "$(pick_python || echo 3.12)" && uv pip install --quiet --python .venv/bin/python -e ".[dev]")
else
  PY="$(pick_python)" || { echo "Python 3.10+ is required (brew install python@3.12)"; exit 1; }
  (cd backend && "$PY" -m venv .venv && .venv/bin/pip install --quiet --upgrade pip && .venv/bin/pip install --quiet -e ".[dev]")
fi

echo "==> Frontend"
command -v npm >/dev/null 2>&1 || { echo "Node.js 18+ is required (brew install node)"; exit 1; }
(cd frontend && npm install --silent)

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from .env.example"
fi

echo
echo "Setup done. Add ANTHROPIC_API_KEY to .env, then run: make dev"
