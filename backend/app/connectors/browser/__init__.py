"""Logged-in browser job collectors registry (LinkedIn, Gupy, InfoJobs, Google)."""

from __future__ import annotations

from backend.app.connectors.browser.base import (
    BrowserCollector,
    BrowserDiscoveryPlan,
    CollectorBlockedError,
)
from backend.app.connectors.browser.google import GoogleCareersBrowserCollector
from backend.app.connectors.browser.gupy import GupyBrowserCollector
from backend.app.connectors.browser.infojobs import InfoJobsBrowserCollector
from backend.app.connectors.browser.linkedin import LinkedInBrowserCollector

ENABLED_BROWSER_SOURCES: tuple[str, ...] = ("linkedin", "gupy", "infojobs", "google")


def build_browser_collectors() -> list[BrowserCollector]:
    """Instantiate all supported browser collectors."""
    return [
        LinkedInBrowserCollector(),
        GupyBrowserCollector(),
        InfoJobsBrowserCollector(),
        GoogleCareersBrowserCollector(),
    ]


__all__ = [
    "BrowserCollector",
    "BrowserDiscoveryPlan",
    "CollectorBlockedError",
    "ENABLED_BROWSER_SOURCES",
    "GoogleCareersBrowserCollector",
    "GupyBrowserCollector",
    "InfoJobsBrowserCollector",
    "LinkedInBrowserCollector",
    "build_browser_collectors",
]
