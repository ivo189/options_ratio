#!/bin/bash
set -e

echo ""
echo "  Options Ratio Screener"
echo "  ─────────────────────"

# ── Python check ────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "  ERROR: python3 not found. Install Python 3.10+ from https://python.org"
  exit 1
fi

PYTHON=$(command -v python3)
echo "  Python: $($PYTHON --version)"

# ── Install/upgrade deps silently ────────────────────────────────────────────
echo "  Installing dependencies…"
$PYTHON -m pip install -q -r requirements.txt

# ── Create .env if missing ────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  Created .env from .env.example (default: TWS paper port 7497)"
fi

# ── Pick port (avoid macOS AirPlay on 5000) ──────────────────────────────────
PORT=${DASHBOARD_PORT:-8080}

echo ""
echo "  Dashboard → http://localhost:$PORT"
echo "  Press Ctrl+C to stop."
echo ""

DASHBOARD_PORT=$PORT $PYTHON app.py
