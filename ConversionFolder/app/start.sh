#!/bin/bash
# Informatica Conversion Tool — Start Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check for .env
if [ ! -f ".env" ]; then
  echo "⚠️  No .env file found. Copying .env.example → .env"
  cp .env.example .env
  echo "📝 Edit .env and add your ANTHROPIC_API_KEY, then re-run this script."
  exit 1
fi

# Check API key is set
source .env
if [ -z "$ANTHROPIC_API_KEY" ] || [ "$ANTHROPIC_API_KEY" = "your_api_key_here" ]; then
  echo "❌ ANTHROPIC_API_KEY is not set in .env"
  exit 1
fi

echo ""
echo "🚀 Starting Informatica Conversion Tool..."
echo "📡 API:  http://localhost:8000/docs"
echo "🌐 UI:   http://localhost:8000"
echo ""

python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
