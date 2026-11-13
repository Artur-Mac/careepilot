"""Playwright browser automation package placeholder until Phase 2/3 CDP copilot."""

from __future__ import annotations

from backend.app.core.config import Settings


class BrowserDriver:
    """Placeholder driver for Phase 1; Phase 2/3 adds shared CDP session and step fill."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
