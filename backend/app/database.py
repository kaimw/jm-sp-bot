from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy.engine import make_url
from sqlalchemy import create_engine, event, inspect, text
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
        return {"connect_args": {"check_same_thread": False, "timeout": 30}}
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


@event.listens_for(engine, "connect")
def _set_sqlite_busy_timeout(dbapi_connection, connection_record) -> None:
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


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
                "next_retry_at": _datetime_type(),
                "started_at": _datetime_type(),
                "version": "INTEGER DEFAULT 0 NOT NULL",
            },
        )
    if "mail_messages" in tables:
        _ensure_columns(
            "mail_messages",
            {
                "received_at": _datetime_type(),
            },
        )
    if "promotion_rules" in tables:
        _ensure_columns(
            "promotion_rules",
            {
                "sku_uuid": "VARCHAR(36)",
            },
        )
    if "delivery_notices" in tables:
        _ensure_columns(
            "delivery_notices",
            {
                "oms_method": "VARCHAR(64) DEFAULT 'wms.order.create' NOT NULL",
                "oms_order_no": "VARCHAR(128)",
                "notice_version": "INTEGER DEFAULT 1 NOT NULL",
                "source_snapshot_hash": "VARCHAR(128)",
                "owner_code": "VARCHAR(128)",
                "warehouse_code": "VARCHAR(128)",
                "shop_code": "VARCHAR(128)",
                "logistic_code": "VARCHAR(128)",
                "waybill_no": "VARCHAR(128)",
                "print_status": "VARCHAR(32) DEFAULT 'NotRequested' NOT NULL",
                "print_error": "TEXT",
                "print_retry_count": "INTEGER DEFAULT 0 NOT NULL",
                "platform_fulfillment_status": "VARCHAR(32) DEFAULT 'NotRequired' NOT NULL",
                "platform_fulfillment_error": "TEXT",
                "platform_fulfillment_retry_count": "INTEGER DEFAULT 0 NOT NULL",
                "platform_fulfillment_synced_at": _datetime_type(),
                "platform_fulfillment_synced_waybill_no": "VARCHAR(128)",
                "split_preview_json": "TEXT DEFAULT '{}' NOT NULL",
                "confirmed_by": "VARCHAR(128)",
                "confirmed_at": _datetime_type(),
            },
        )
    if "middle_platform_orders" in tables:
        _ensure_columns(
            "middle_platform_orders",
            {
                "source_policy": "VARCHAR(32) DEFAULT 'CRM_ONLY' NOT NULL",
                "platform_order_no": "VARCHAR(128)",
                "shop_code": "VARCHAR(128)",
                "channel_code": "VARCHAR(128)",
                "fulfillment_type": "VARCHAR(64)",
                # V2 Phase 1 新增字段
                "order_type": "VARCHAR(32)",
                "entity_code": "VARCHAR(32)",
                "fulfillment_entity": "VARCHAR(32)",
                "erp_bill_no": "VARCHAR(64)",
            },
        )
    if "middle_platform_order_items" in tables:
        _ensure_columns(
            "middle_platform_order_items",
            {
                "shop_sku_code": "VARCHAR(128)",
                "channel_code": "VARCHAR(128)",
            },
        )
    if "crm_sales_orders" in tables:
        _ensure_columns(
            "crm_sales_orders",
            {
                "latest_snapshot_id": "VARCHAR(36)",
                "scope_status": "VARCHAR(32) DEFAULT 'InScope' NOT NULL",
                "scope_ignore_reason": "TEXT",
                "receipt_phone": "VARCHAR(64)",
            },
        )
    if "exception_cases" in tables:
        _ensure_columns(
            "exception_cases",
            {
                "assignee": "VARCHAR(128)",
                "resolution_note": "TEXT",
                "resolution_evidence_json": "TEXT",
                "due_at": _datetime_type(),
                "resolved_at": _datetime_type(),
                "reopened_at": _datetime_type(),
                "last_actor": "VARCHAR(128)",
                "updated_at": _datetime_type(),
            },
        )
    if "agent_run_logs" in tables:
        _ensure_columns(
            "agent_run_logs",
            {
                "agent_name": "VARCHAR(64) NOT NULL",
                "task_type": "VARCHAR(64) NOT NULL",
                "related_object_type": "VARCHAR(64)",
                "related_object_id": "VARCHAR(36)",
                "input_json": "TEXT",
                "output_json": "TEXT",
                "status": "VARCHAR(32) NOT NULL",
                "error_message": "TEXT",
                "started_at": _datetime_type(),
                "finished_at": _datetime_type(),
            },
        )
    if "production_tasks" in tables:
        _ensure_columns(
            "production_tasks",
            {
                "version": "INTEGER DEFAULT 0 NOT NULL",
            },
        )

    # V2 Phase 1 新表（如果尚未创建，Base.metadata.create_all 会自动创建）
    for new_table in ("order_sequences", "entity_mappings", "customer_entity_mappings",
                      "inter_entity_transfers", "mail_receiver_configs",
                      "product_prices", "inventory_import_records",
                      "inventory_snapshot_histories"):
        if new_table not in tables:
            import logging
            logging.getLogger(__name__).warning("新表 %s 尚未创建，将在下次重启时自动创建", new_table)

    if "product_inventory_snapshots" in tables:
        _ensure_indexes(
            {
                "ix_inventory_warehouse_material": "CREATE INDEX IF NOT EXISTS ix_inventory_warehouse_material ON product_inventory_snapshots (warehouse_code, material_code)",
                "ix_inventory_qty_warehouse_material": "CREATE INDEX IF NOT EXISTS ix_inventory_qty_warehouse_material ON product_inventory_snapshots (qty, warehouse_code, material_code)",
            }
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


def _ensure_indexes(index_sql: dict[str, str]) -> None:
    inspector = inspect(engine)
    existing: set[str] = set()
    for table_name in inspector.get_table_names():
        existing.update(index["name"] for index in inspector.get_indexes(table_name) if index.get("name"))
    missing = [sql for name, sql in index_sql.items() if name not in existing]
    if not missing:
        return
    with engine.begin() as connection:
        for sql in missing:
            connection.execute(text(sql))


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
