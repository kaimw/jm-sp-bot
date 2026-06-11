from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine

from backend.app import models  # noqa: F401
from backend.app.database import Base, engine_kwargs, mask_database_url, normalize_database_url

MIGRATION_TABLE_ORDER = [
    "system_configs",
    "mail_templates",
    "production_departments",
    "logistics_departments",
    "model_provider_configs",
    "workflow_definitions",
    "workflow_versions",
    "workflow_import_jobs",
    "mail_messages",
    "attachment_assets",
    "processing_jobs",
    "model_call_logs",
    "maintenance_sessions",
    "maintenance_actions",
    "order_requirements",
    "requirement_workflow_bindings",
    "logistics_tasks",
    "logistics_task_versions",
    "fulfillment_items",
    "extraction_evidence",
    "production_tasks",
    "production_task_versions",
    "question_and_replies",
    "outbound_mail_jobs",
    "exception_cases",
    "audit_events",
    "mail_workflow_matches",
    "cleanup_jobs",
    "backup_jobs",
    "crm_sales_orders",
    "crm_order_items",
    "crm_sync_runs",
    "product_spus",
    "product_skus",
    "product_inventory_snapshots",
    "channel_pricings",
    "promotion_rules",
]

DEFERRED_COLUMN_UPDATES = {
    "mail_messages": ["related_task_id"],
    "attachment_assets": ["parent_attachment_id"],
}


@dataclass(frozen=True)
class MigrationResult:
    source_url: str
    target_url: str
    execute: bool
    truncate: bool
    table_counts: dict[str, int]
    inserted_counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_url": mask_database_url(self.source_url),
            "target_url": mask_database_url(self.target_url),
            "execute": self.execute,
            "truncate": self.truncate,
            "table_counts": self.table_counts,
            "inserted_counts": self.inserted_counts,
        }


def build_engine(database_url: str) -> Engine:
    normalized = normalize_database_url(database_url)
    return create_engine(normalized, **engine_kwargs(normalized))


def migration_tables() -> list[Any]:
    known_names = set(Base.metadata.tables)
    missing = sorted(known_names - set(MIGRATION_TABLE_ORDER))
    unknown = sorted(set(MIGRATION_TABLE_ORDER) - known_names)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing tables: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown tables: {', '.join(unknown)}")
        raise RuntimeError(f"migration table order is invalid; {'; '.join(details)}")
    return [Base.metadata.tables[name] for name in MIGRATION_TABLE_ORDER]


def sqlite_file_exists(database_url: str) -> bool:
    if database_url == "sqlite:///:memory:":
        return True
    if not database_url.startswith("sqlite:///"):
        return False
    raw = database_url.removeprefix("sqlite:///")
    return Path(raw).exists()


def count_tables(engine: Engine) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in migration_tables():
            counts[table.name] = int(conn.execute(select(func.count()).select_from(table)).scalar_one())
    return counts


def read_table_rows(engine: Engine, table_name: str, batch_size: int = 500) -> list[dict[str, Any]]:
    table = Base.metadata.tables[table_name]
    rows: list[dict[str, Any]] = []
    with engine.connect() as conn:
        result = conn.execute(select(table)).mappings()
        batch: list[dict[str, Any]] = []
        for row in result:
            batch.append(dict(row))
            if len(batch) >= batch_size:
                rows.extend(batch)
                batch = []
        rows.extend(batch)
    return rows


def assert_empty_target(target_engine: Engine) -> None:
    counts = count_tables(target_engine)
    occupied = {name: count for name, count in counts.items() if count}
    if occupied:
        detail = ", ".join(f"{name}={count}" for name, count in sorted(occupied.items()))
        raise RuntimeError(f"target database is not empty; pass --truncate-target to replace existing data: {detail}")


def migrate_sqlite_to_database(
    source_url: str,
    target_url: str,
    *,
    execute: bool = False,
    truncate_target: bool = False,
) -> MigrationResult:
    normalized_source = normalize_database_url(source_url)
    normalized_target = normalize_database_url(target_url)
    if not normalized_source.startswith("sqlite:///") and normalized_source != "sqlite:///:memory:":
        raise ValueError("source must be a sqlite database URL")
    if normalized_source == normalized_target:
        raise ValueError("source and target database URLs must be different")
    if not sqlite_file_exists(normalized_source):
        raise FileNotFoundError(f"source sqlite database does not exist: {mask_database_url(normalized_source)}")

    source_engine = build_engine(normalized_source)
    source_counts = count_tables(source_engine)

    if not execute:
        return MigrationResult(
            source_url=normalized_source,
            target_url=normalized_target,
            execute=False,
            truncate=False,
            table_counts=source_counts,
            inserted_counts={name: 0 for name in source_counts},
        )

    target_engine = build_engine(normalized_target)
    Base.metadata.create_all(bind=target_engine)
    if truncate_target:
        with target_engine.begin() as conn:
            for table_name, columns in DEFERRED_COLUMN_UPDATES.items():
                table = Base.metadata.tables[table_name]
                conn.execute(table.update().values(**dict.fromkeys(columns)))
            for table in reversed(migration_tables()):
                conn.execute(table.delete())
    else:
        assert_empty_target(target_engine)

    inserted_counts: dict[str, int] = {}
    deferred_updates: dict[str, list[dict[str, Any]]] = {}
    with target_engine.begin() as conn:
        for table in migration_tables():
            rows = read_table_rows(source_engine, table.name)
            deferred_columns = DEFERRED_COLUMN_UPDATES.get(table.name, [])
            if deferred_columns and rows:
                pending_updates = []
                normalized_rows = []
                for row in rows:
                    deferred_values = {column: row.get(column) for column in deferred_columns}
                    if any(value is not None for value in deferred_values.values()):
                        pending_updates.append({"id": row["id"], **deferred_values})
                    normalized_rows.append({**row, **dict.fromkeys(deferred_columns)})
                deferred_updates[table.name] = pending_updates
                rows = normalized_rows
            if rows:
                conn.execute(table.insert(), rows)
            inserted_counts[table.name] = len(rows)

        for table_name, updates in deferred_updates.items():
            table = Base.metadata.tables[table_name]
            for update in updates:
                row_id = update.pop("id")
                conn.execute(table.update().where(table.c.id == row_id).values(**update))

    return MigrationResult(
        source_url=normalized_source,
        target_url=normalized_target,
        execute=True,
        truncate=truncate_target,
        table_counts=source_counts,
        inserted_counts=inserted_counts,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate the local SQLite MVP database to another SQLAlchemy database URL.")
    parser.add_argument("--source", default="sqlite:///data/app.db", help="Source SQLite database URL. Default: sqlite:///data/app.db")
    parser.add_argument("--target", required=True, help="Target SQLAlchemy database URL, e.g. postgresql+psycopg://user:pass@host/db")
    parser.add_argument("--execute", action="store_true", help="Actually create target tables and copy data. Omit for dry-run counts.")
    parser.add_argument("--truncate-target", action="store_true", help="Delete all target table rows before inserting source rows.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = migrate_sqlite_to_database(
        args.source,
        args.target,
        execute=args.execute,
        truncate_target=args.truncate_target,
    )
    print(result.as_dict())


if __name__ == "__main__":
    main()
