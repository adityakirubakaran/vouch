#!/usr/bin/env bash
# Backend on :8000 with reload, Vite dev server on :5173. Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x backend/.venv/bin/uvicorn ] || { echo "Run make setup first"; exit 1; }

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(cd backend && .venv/bin/uvicorn vouch.main:app --port 8000 --reload --reload-dir vouch) &
(cd frontend && npm run dev -- --port 5173 --strictPort) &
sleep 2
echo
echo "Vouch is running at http://localhost:5173  (API on :8000)"
wait
