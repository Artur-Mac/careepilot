#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p data/real

uv run --locked python - <<'PY'
from pathlib import Path
from secrets import randbelow, token_urlsafe

credentials = Path("data/real/access.txt")
try:
    with credentials.open("x") as file:
        file.write(f"{randbelow(100_000_000):08d}\n{token_urlsafe(48)}\n")
except FileExistsError:
    pass
PY

{
  IFS= read -r CAREERPILOT_PASSCODE
  IFS= read -r CAREERPILOT_SESSION_SECRET
} < data/real/access.txt
export CAREERPILOT_PASSCODE CAREERPILOT_SESSION_SECRET
export DATABASE_URL=sqlite:///./data/real/careerpilot.db
export ARTIFACTS_DIR=./data/real/artifacts
export TEST_ATS_ENABLED=false DRY_RUN=true LIVE_MODE_ENABLED=false
export CAREERPILOT_HOST=127.0.0.1 CAREERPILOT_PORT=8000
export PYTHONPATH=.
uv run --locked alembic upgrade head
exec uv run --locked uvicorn backend.app.main:app --host "$CAREERPILOT_HOST" --port "$CAREERPILOT_PORT"
