"""OpenCode CLI + browse extension orchestration for CareerPilot."""

from backend.app.services.opencode.service import OpenCodeService, OpenCodeUnavailableError

__all__ = ["OpenCodeService", "OpenCodeUnavailableError"]
