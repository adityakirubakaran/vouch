#!/usr/bin/env bash
# Expose localhost:8000 over https so Twilio can reach the voice webhooks.
# Copy the printed https URL into PUBLIC_BASE_URL in .env, then restart Vouch.
set -euo pipefail
if command -v cloudflared >/dev/null 2>&1; then
  exec cloudflared tunnel --url http://localhost:8000
elif command -v ngrok >/dev/null 2>&1; then
  exec ngrok http 8000
else
  echo "Install a tunnel first: brew install cloudflared  (no account needed)"
  exit 1
fi
