#!/usr/bin/env bash
# One-shot setup for a fresh clone of stock-buzz.
#
# Usage: ./setup.sh
#
# Idempotent — safe to re-run. Skips steps that are already complete.
set -euo pipefail

cd "$(dirname "$0")"

echo "=== stock-buzz setup ==="

# 1. Python version check
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.10+ first." >&2
  exit 1
fi
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  echo "ERROR: Python $PY_MAJOR.$PY_MINOR is too old. Need 3.10+." >&2
  exit 1
fi
echo "✓ Python $(python3 --version | cut -d' ' -f2)"

# 2. Virtual env
if [ ! -d venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi
echo "✓ venv ready"

# 3. Install dependencies
# shellcheck disable=SC1091
source venv/bin/activate
echo "Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "✓ dependencies installed"

# 4. .env template
if [ ! -f .env ]; then
  cp .env.example .env
  echo "✓ created .env from template"
  echo
  echo "  → NEXT STEP: edit .env to add your API keys."
  echo "    At minimum, set ONE of:"
  echo "      ANTHROPIC_API_KEY=sk-ant-...         (direct Anthropic API)"
  echo "    OR"
  echo "      STOCK_BUZZ_PROVIDER=bedrock          (AWS Bedrock)"
  echo "      AWS_PROFILE=your-profile"
  echo "      AWS_REGION=us-east-1"
  echo
  echo "    Optional but recommended:"
  echo "      FINNHUB_API_KEY=...                  (free at finnhub.io)"
else
  echo "✓ .env already exists (not overwriting)"
fi

# 5. Ensure data + output dirs exist
mkdir -p data output

echo
echo "=== Setup complete ==="
echo
echo "Run the dashboard:"
echo "  source venv/bin/activate"
echo "  python -m src.server"
echo
echo "Then open http://localhost:8765/ in your browser."
