from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy.engine import make_url
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.config import settings


class Base(DeclarativeBase):
    pass


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def mask_database_url(database_url: str) -> str:
    try:
        return make_url(normalize_database_url(database_url)).render_as_string(hide_password=True)
    except Exception:
        return "<invalid database url>"


def database_runtime_info() -> dict:
    url = make_url(database_url)
    return {
        "dialect": url.get_backend_name(),
        "driver": url.get_driver_name(),
        "url": url.render_as_string(hide_password=True),
    }


def engine_kwargs(database_url: str) -> dict:
    if database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


def _sqlite_path(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite:///"):
        return None
    raw = database_url.removeprefix("sqlite:///")
    if raw == ":memory:":
        return None
    return Path(raw)


database_url = normalize_database_url(settings.database_url)

db_path = _sqlite_path(database_url)
if db_path is not None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(database_url, **engine_kwargs(database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from backend.app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_runtime_schema()


def ensure_runtime_schema() -> None:
    """Add nullable runtime columns for existing deployments without a migration tool."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "outbound_mail_jobs" in tables:
        _ensure_columns(
            "outbound_mail_jobs",
            {
                "attempt_count": "INTEGER DEFAULT 0",
                "next_retry_at": _datetime_type(),
                "last_error": "VARCHAR(1000)",
                "priority": "INTEGER DEFAULT 40",
                "locked_by": "VARCHAR(128)",
                "locked_until": _datetime_type(),
                "sending_started_at": _datetime_type(),
                "sent_at": _datetime_type(),
            },
        )
    if "processing_jobs" in tables:
        _ensure_columns(
            "processing_jobs",
            {
                "locked_by": "VARCHAR(128)",
                "locked_until": _datetime_type(),
                "started_at": _datetime_type(),
            },
        )
    if "mail_messages" in tables:
        _ensure_columns(
            "mail_messages",
            {
                "received_at": _datetime_type(),
            },
        )


def _datetime_type() -> str:
    if engine.dialect.name == "postgresql":
        return "TIMESTAMP WITH TIME ZONE"
    return "DATETIME"


def _ensure_columns(table_name: str, columns: dict[str, str]) -> None:
    inspector = inspect(engine)
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    missing = [(name, sql_type) for name, sql_type in columns.items() if name not in existing]
    if not missing:
        return
    with engine.begin() as connection:
        for name, sql_type in missing:
            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {sql_type}"))


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
