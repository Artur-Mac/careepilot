"""Centralized, transactional, idempotent state machine for CareerPilot applications."""

from __future__ import annotations

from enum import StrEnum


class ApplicationStatus(StrEnum):
    """Exact frozen lifecycle states for an application."""

    DISCOVERED = "discovered"
    EVALUATED = "evaluated"
    PREPARED = "prepared"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PREPARING_FORM = "preparing_form"
    READY_FOR_MANUAL_REVIEW = "ready_for_manual_review"
    SUBMITTED_UNVERIFIED = "submitted_unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"
    EXPIRED = "expired"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    FAILED = "failed"
    NEEDS_USER_INPUT = "needs_user_input"


ALLOWED_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.DISCOVERED: frozenset(
        {
            ApplicationStatus.EVALUATED,
            ApplicationStatus.PREPARED,
            ApplicationStatus.REJECTED,
            ApplicationStatus.CANCELED,
        }
    ),
    ApplicationStatus.EVALUATED: frozenset(
        {
            ApplicationStatus.PREPARED,
            ApplicationStatus.NEEDS_USER_INPUT,
            ApplicationStatus.REJECTED,
            ApplicationStatus.CANCELED,
        }
    ),
    ApplicationStatus.PREPARED: frozenset(
        {
            ApplicationStatus.AWAITING_APPROVAL,
            ApplicationStatus.NEEDS_USER_INPUT,
            ApplicationStatus.BLOCKED,
            ApplicationStatus.EXPIRED,
            ApplicationStatus.CANCELED,
        }
    ),
    ApplicationStatus.AWAITING_APPROVAL: frozenset(
        {
            ApplicationStatus.APPROVED,
            ApplicationStatus.REJECTED,
            ApplicationStatus.EXPIRED,
            ApplicationStatus.NEEDS_USER_INPUT,
            ApplicationStatus.BLOCKED,
            ApplicationStatus.CANCELED,
        }
    ),
    ApplicationStatus.NEEDS_USER_INPUT: frozenset(
        {
            ApplicationStatus.PREPARED,
            ApplicationStatus.AWAITING_APPROVAL,
            ApplicationStatus.READY_FOR_MANUAL_REVIEW,
            ApplicationStatus.BLOCKED,
            ApplicationStatus.CANCELED,
        }
    ),
    ApplicationStatus.APPROVED: frozenset(
        {
            ApplicationStatus.PREPARING_FORM,
            ApplicationStatus.EXPIRED,
            ApplicationStatus.BLOCKED,
            ApplicationStatus.CANCELED,
            ApplicationStatus.FAILED,
        }
    ),
    ApplicationStatus.PREPARING_FORM: frozenset(
        {
            ApplicationStatus.READY_FOR_MANUAL_REVIEW,
            ApplicationStatus.SUBMITTED_UNVERIFIED,
            ApplicationStatus.NEEDS_USER_INPUT,
            ApplicationStatus.BLOCKED,
            ApplicationStatus.FAILED,
            ApplicationStatus.EXPIRED,
            ApplicationStatus.CANCELED,
        }
    ),
    ApplicationStatus.READY_FOR_MANUAL_REVIEW: frozenset(
        {
            ApplicationStatus.SUBMITTED_UNVERIFIED,
            ApplicationStatus.VERIFIED,
            ApplicationStatus.BLOCKED,
            ApplicationStatus.CANCELED,
        }
    ),
    ApplicationStatus.SUBMITTED_UNVERIFIED: frozenset(
        {
            ApplicationStatus.VERIFIED,
            ApplicationStatus.BLOCKED,
            ApplicationStatus.FAILED,
        }
    ),
    ApplicationStatus.FAILED: frozenset(
        {
            ApplicationStatus.PREPARING_FORM,
            ApplicationStatus.AWAITING_APPROVAL,
            ApplicationStatus.CANCELED,
        }
    ),
    ApplicationStatus.BLOCKED: frozenset(
        {
            ApplicationStatus.AWAITING_APPROVAL,
            ApplicationStatus.READY_FOR_MANUAL_REVIEW,
            ApplicationStatus.CANCELED,
        }
    ),
    ApplicationStatus.REJECTED: frozenset(),
    ApplicationStatus.EXPIRED: frozenset(),
    ApplicationStatus.CANCELED: frozenset(),
    ApplicationStatus.VERIFIED: frozenset(),
}

TERMINAL_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {
        ApplicationStatus.REJECTED,
        ApplicationStatus.EXPIRED,
        ApplicationStatus.CANCELED,
        ApplicationStatus.VERIFIED,
    }
)


class InvalidTransitionError(ValueError):
    """Raised when an application state transition violates the centralized state machine."""


def validate_transition(
    current: ApplicationStatus | str,
    target: ApplicationStatus | str,
    *,
    allow_idempotent: bool = True,
) -> ApplicationStatus:
    """Validate state transition centrally; raises InvalidTransitionError on illegal jumps."""
    curr = ApplicationStatus(current)
    nxt = ApplicationStatus(target)
    if curr == nxt:
        if allow_idempotent:
            return nxt
        raise InvalidTransitionError(f"Transição idempotente desabilitada para {curr.value}")
    if nxt not in ALLOWED_TRANSITIONS[curr]:
        raise InvalidTransitionError(
            f"Transição proibida na máquina de estados: {curr.value} -> {nxt.value}"
        )
    return nxt
