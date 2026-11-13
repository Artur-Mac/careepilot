#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

DATA_DIR="${CAREERPILOT_DATA_DIR:-data/dev}"
mkdir -p "$DATA_DIR" "$DATA_DIR/artifacts"

export DATABASE_URL="${DATABASE_URL:-sqlite:///${DATA_DIR}/careerpilot.db}"
export ARTIFACTS_DIR="${ARTIFACTS_DIR:-${DATA_DIR}/artifacts}"
export CAREERPILOT_PASSCODE="${CAREERPILOT_PASSCODE:-demo-local-2026}"
export CAREERPILOT_HOST="${CAREERPILOT_HOST:-127.0.0.1}"
export CAREERPILOT_PORT="${CAREERPILOT_PORT:-8000}"
export PYTHONPATH=.

echo "Aplicando migrações Alembic (SQLite WAL) em ${DATA_DIR}..."
uv run --locked python -m backend.app.db.migrations upgrade

echo "Iniciando CareerPilot em http://${CAREERPILOT_HOST}:${CAREERPILOT_PORT} ..."
exec uv run --locked uvicorn backend.app.main:app --host "$CAREERPILOT_HOST" --port "$CAREERPILOT_PORT"
