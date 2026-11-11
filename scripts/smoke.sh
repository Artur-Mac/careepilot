#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH=.

echo "[1/4] Verificando lint (ruff)..."
uv run --locked --extra dev ruff check backend scripts

echo "[2/4] Verificando tipos (mypy)..."
uv run --locked --extra dev mypy backend/app

echo "[3/4] Rodando suíte completa (unit + integration + security + e2e Playwright)..."
uv run --locked --extra dev pytest

echo "[4/4] Smoke test OK!"
