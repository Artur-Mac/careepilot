#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

DATA_DIR="${CAREERPILOT_DATA_DIR:-data/real}"
mkdir -p "$DATA_DIR" "$DATA_DIR/artifacts"

CREDENTIALS_FILE="$DATA_DIR/access.txt"
export CREDENTIALS_FILE
uv run --locked python - <<'PY'
import os
from pathlib import Path
from secrets import randbelow, token_urlsafe

credentials = Path(os.environ["CREDENTIALS_FILE"])
try:
    with credentials.open("x") as file:
        file.write(f"{randbelow(100_000_000):08d}\n{token_urlsafe(48)}\n")
except FileExistsError:
    pass
PY

{
  IFS= read -r PASSCODE_FROM_FILE
  IFS= read -r SECRET_FROM_FILE
} < "$CREDENTIALS_FILE"

export CAREERPILOT_PASSCODE="${CAREERPILOT_PASSCODE:-$PASSCODE_FROM_FILE}"
export CAREERPILOT_SESSION_SECRET="${CAREERPILOT_SESSION_SECRET:-$SECRET_FROM_FILE}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///${DATA_DIR}/careerpilot.db}"
export ARTIFACTS_DIR="${ARTIFACTS_DIR:-${DATA_DIR}/artifacts}"
export CAREERPILOT_HOST="${CAREERPILOT_HOST:-127.0.0.1}"
export CAREERPILOT_PORT="${CAREERPILOT_PORT:-8000}"
export PYTHONPATH=.

uv run --locked python -m backend.app.db.migrations upgrade
exec uv run --locked uvicorn backend.app.main:app --host "$CAREERPILOT_HOST" --port "$CAREERPILOT_PORT"
