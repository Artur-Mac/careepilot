"""SQLAlchemy 2 declarative base."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base ORM class for all CareerPilot models."""
