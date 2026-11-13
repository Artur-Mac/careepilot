"""Application lifecycle statuses for the CareerPilot human-in-the-loop workflow."""

from __future__ import annotations

from enum import StrEnum


class ApplicationStatus(StrEnum):
    """Simplified Phase 1-3 lifecycle states."""

    READY = "ready"
    REVIEW = "review"
    NEEDS_USER = "needs_user"
    DONE = "done"
    CLOSED = "closed"


RESUMABLE_STATUSES: frozenset[str] = frozenset(
    {
        ApplicationStatus.READY.value,
        ApplicationStatus.REVIEW.value,
        ApplicationStatus.NEEDS_USER.value,
    }
)
ACTIVE_PANEL_STATUSES: frozenset[str] = frozenset(
    {
        ApplicationStatus.READY.value,
        ApplicationStatus.REVIEW.value,
        ApplicationStatus.NEEDS_USER.value,
        ApplicationStatus.DONE.value,
    }
)
