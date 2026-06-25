"""SQLAlchemy engine, session factory, and schema bootstrap.

This is the only module that owns the database connection. It is imported by the
repository and the worker/API entrypoints; importing it requires SQLAlchemy
(the ``service`` extra), so the dependency-free core never touches it.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_sessionmaker():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _SessionLocal


def init_db() -> None:
    """Create tables if they do not exist (idempotent).

    Enough for the MVP; a real migration tool (Alembic) can replace this once the
    schema starts evolving in production.
    """
    from . import orm  # noqa: F401 - ensure models are registered on Base

    Base.metadata.create_all(bind=get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commit on success, roll back on error."""
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
