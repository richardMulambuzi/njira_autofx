#!/usr/bin/env bash
set -euo pipefail

echo "🚀 Building and starting Njira Auto FX..."
docker compose down --remove-orphans
docker compose build --no-cache
docker compose up -d

echo "✅ Services running:"
echo "   API:  http://localhost:8000"
echo "   UI:   http://localhost:8100"

docker compose ps