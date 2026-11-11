#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH=.

echo "Aplicando migrações Alembic (SQLite WAL)..."
uv run alembic upgrade head

echo "Iniciando ATS Fictício Local em 127.0.0.1:8010..."
uv run python scripts/run_fake_ats.py &
ATS_PID=$!
trap 'kill ${ATS_PID} 2>/dev/null || true' EXIT

echo "Iniciando CareerPilot MVP em http://127.0.0.1:8000 ..."
exec uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
