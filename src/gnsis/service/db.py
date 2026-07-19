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


#: Additive, idempotent column migrations for tables that predate a change.
#: ``create_all`` only creates *missing tables*, never new columns on existing
#: ones, so schema changes to a live table are applied here. Each entry is
#: ``(table, column, column_type_sql)`` and is applied with ADD COLUMN IF NOT
#: EXISTS — safe to run on every deploy, preserves existing rows.
_ADDITIVE_COLUMNS = [
    ("jobs", "workspace_id", "VARCHAR(64)"),
    ("jobs", "repository_id", "VARCHAR(64)"),
    # LiteLLM correlation key on the existing model-call table.
    ("execution_model_calls", "event_id", "VARCHAR(64)"),
    # Ledger-integrity fields on the append-only usage ledger (PR-G1): explicit
    # idempotency, provider request id, dual cost, reconciliation state.
    ("usage_records", "idempotency_key", "VARCHAR(191)"),
    ("usage_records", "provider_request_id", "VARCHAR(191)"),
    ("usage_records", "genesis_calculated_cost", "VARCHAR(40)"),
    ("usage_records", "cost_source", "VARCHAR(24)"),
    ("usage_records", "reconciliation_state", "VARCHAR(24)"),
    ("usage_records", "error_category", "VARCHAR(48)"),
    # Versioned pricing (G3).
    ("usage_records", "reconciliation_reason", "VARCHAR(48)"),
    ("usage_records", "pricing_version_id", "VARCHAR(64)"),
    # Public gateway attribution (G4).
    ("usage_records", "project_id", "VARCHAR(64)"),
    ("usage_records", "virtual_key_id", "VARCHAR(64)"),
]


def _apply_additive_columns(engine) -> None:
    from sqlalchemy import text

    dialect = engine.dialect.name
    with engine.begin() as conn:
        for table, column, coltype in _ADDITIVE_COLUMNS:
            if dialect == "sqlite":
                # SQLite lacks ADD COLUMN IF NOT EXISTS; check pragma first.
                cols = {
                    row[1]
                    for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
                }
                if column not in cols:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"
                    )
            else:
                conn.execute(
                    text(
                        f"ALTER TABLE {table} "
                        f"ADD COLUMN IF NOT EXISTS {column} {coltype}"
                    )
                )


def init_db() -> None:
    """Create/upgrade the schema (idempotent). Safe to run on every deploy.

    Two steps: ``create_all`` adds any brand-new tables, then a small set of
    additive ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` statements bring
    pre-existing tables (currently ``jobs``) up to date without touching data.
    This is the repository's migration mechanism; it runs via the
    ``gnsis-migrate`` release hook and the API/worker startup.
    """
    from . import orm  # noqa: F401 - ensure models are registered on Base

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _apply_additive_columns(engine)


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
