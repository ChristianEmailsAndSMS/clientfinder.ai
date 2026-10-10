from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db():
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def add_ignoring_conflict(session, obj) -> bool:
    """Insert inside a savepoint.

    A unique conflict rolls back only the savepoint and returns False.
    Any other flush error also rolls the savepoint back, then re-raises,
    so one bad row does not poison the surrounding transaction.
    """
    sp = session.begin_nested()
    try:
        session.add(obj)
        session.flush()
        sp.commit()
        return True
    except IntegrityError:
        sp.rollback()
        return False
    except Exception:
        sp.rollback()
        raise
