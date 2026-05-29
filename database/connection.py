"""
SQLAlchemy engine / session factory.

Usage:
    from database.connection import get_session

    with get_session() as session:
        session.add(...)
        session.commit()
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from config.settings import settings

# Use connection pool tailored for a long-running single-process bot.
_engine = create_engine(
    settings.database_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,          # detect stale connections automatically
    pool_recycle=3600,           # recycle connections every hour
    echo=False,                  # set True only during debug sessions
)

_SessionFactory = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Provide a transactional database session."""
    session: Session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create all tables if they do not exist yet."""
    from database.models import Base  # local import to avoid circular deps
    Base.metadata.create_all(_engine)
