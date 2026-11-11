"""Launch the fictional local employer ATS web server on loopback (TEST DATA ONLY)."""

from __future__ import annotations

import os
from urllib.parse import urlparse

import uvicorn

from backend.app.browser_automation.fake_ats import fake_ats_app


def main() -> None:
    raw_url = os.getenv("TEST_ATS_URL", "http://127.0.0.1:8010")
    parsed = urlparse(raw_url)
    host = parsed.hostname or "127.0.0.1"
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"TEST_ATS_URL deve permanecer em loopback, recebido: {raw_url}")
    port = parsed.port or 8010
    uvicorn.run(fake_ats_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
