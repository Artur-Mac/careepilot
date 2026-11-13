#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

DATA_DIR="${CAREERPILOT_DATA_DIR:-data/real}"
PROFILE_DIR="${CAREERPILOT_CHROME_PROFILE_DIR:-${DATA_DIR}/chrome-profile}"
mkdir -p "$PROFILE_DIR"

CDP_URL="${BROWSER_CDP_URL:-http://127.0.0.1:9222}"
CDP_PORT="$(
  uv run --locked python - "$CDP_URL" <<'PY'
import sys
from urllib.parse import urlparse
from backend.app.core.ssrf import validate_cdp_url

parsed = urlparse(validate_cdp_url(sys.argv[1]))
print(parsed.port or 9222)
PY
)"

find_browser() {
  if [[ -n "${CHROME_BIN:-}" && -x "${CHROME_BIN}" ]]; then
    echo "$CHROME_BIN"
    return 0
  fi
  for cand in google-chrome google-chrome-stable chromium chromium-browser brave-browser; do
    if command -v "$cand" >/dev/null 2>&1; then
      command -v "$cand"
      return 0
    fi
  done
  for pw in "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux64/chrome \
            "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux/chrome; do
    if [[ -x "$pw" ]]; then
      echo "$pw"
      return 0
    fi
  done
  if [[ -x "/opt/vivaldi/vivaldi" ]]; then
    echo "/opt/vivaldi/vivaldi"
    return 0
  fi
  return 1
}

if ! BROWSER_EXE="$(find_browser)"; then
  echo "Erro: nenhum navegador Chromium/Chrome encontrado (configure CHROME_BIN=/caminho/chrome)." >&2
  exit 1
fi

echo "Iniciando navegador para CareerPilot (CDP 127.0.0.1:${CDP_PORT})..."
echo "Perfil persistente: ${PROFILE_DIR}"
echo "Faça login no LinkedIn, Gupy e InfoJobs na janela aberta."

exec "$BROWSER_EXE" \
  --remote-debugging-port="$CDP_PORT" \
  --remote-debugging-address=127.0.0.1 \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  "$@"
