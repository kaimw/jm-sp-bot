from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import MailMessage, OrderRequirement, OutboundMailJob, ProductionTask
from backend.app.services.bootstrap import seed_defaults
from backend.app.services.jsonutil import dumps
from scripts.migrate_sqlite_to_database import migrate_sqlite_to_database


def create_source_database(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    seed_defaults(session)
    mail = MailMessage(
        direction="Inbound",
        from_address="sales@jimuyida.com",
        to_json=dumps(["bot.market@jimuyida.com"]),
        cc_json=dumps([]),
        subject="迁移测试邮件",
        body_text="客户名称：迁移测试客户",
        classification="SalesOrderRequirement",
        classification_confidence=90,
        dedupe_key="migration-mail",
    )
    session.add(mail)
    session.flush()

    requirement = OrderRequirement(
        source_mail_id=mail.id,
        internal_order_no="MIG-SO-001",
        customer_name="迁移测试客户",
        salesperson_email="sales@jimuyida.com",
        product_summary="迁移测试产品",
        expected_delivery_date="2026-05-20",
        quantity_text="1套",
        status="TaskCreated",
    )
    session.add(requirement)
    session.flush()

    task = ProductionTask(
        task_no="PT-MIG-001",
        requirement_id=requirement.id,
        status="TaskIssued",
        target_mail_to_json=dumps(["bot.production@jimuyida.com"]),
        target_mail_cc_json=dumps([]),
    )
    session.add(task)
    session.flush()

    mail.related_task_id = task.id
    outbound = OutboundMailJob(
        related_task_id=task.id,
        mail_type="TaskIssue",
        to_json=dumps(["bot.production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="迁移测试外发",
        body="完整外发正文",
        idempotency_key="migration-outbound",
        status="Pending",
    )
    session.add(outbound)
    session.commit()
    session.close()


def test_migrate_sqlite_to_database_dry_run_and_execute(tmp_path: Path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    create_source_database(source)

    dry_run = migrate_sqlite_to_database(f"sqlite:///{source}", f"sqlite:///{target}")
    assert dry_run.execute is False
    assert dry_run.table_counts["mail_messages"] == 1
    assert dry_run.table_counts["order_requirements"] == 1
    assert dry_run.table_counts["production_tasks"] == 1
    assert dry_run.table_counts["outbound_mail_jobs"] == 1
    assert not target.exists()

    result = migrate_sqlite_to_database(f"sqlite:///{source}", f"sqlite:///{target}", execute=True)
    assert result.inserted_counts["mail_messages"] == 1
    assert result.inserted_counts["order_requirements"] == 1
    assert result.inserted_counts["production_tasks"] == 1
    assert result.inserted_counts["outbound_mail_jobs"] == 1

    target_engine = create_engine(f"sqlite:///{target}")
    Session = sessionmaker(bind=target_engine, autoflush=False, expire_on_commit=False)
    session = Session()
    migrated_mail = session.query(MailMessage).filter_by(dedupe_key="migration-mail").one()
    migrated_task = session.query(ProductionTask).filter_by(task_no="PT-MIG-001").one()
    assert migrated_mail.subject == "迁移测试邮件"
    assert migrated_mail.related_task_id == migrated_task.id
    assert session.query(OrderRequirement).filter_by(internal_order_no="MIG-SO-001").one().source_mail_id == migrated_mail.id
    assert session.query(OutboundMailJob).filter_by(idempotency_key="migration-outbound").one().related_task_id == migrated_task.id
    session.close()


def test_migrate_sqlite_to_database_refuses_non_empty_target(tmp_path: Path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    create_source_database(source)
    create_source_database(target)

    with pytest.raises(RuntimeError, match="target database is not empty"):
        migrate_sqlite_to_database(f"sqlite:///{source}", f"sqlite:///{target}", execute=True)

    result = migrate_sqlite_to_database(f"sqlite:///{source}", f"sqlite:///{target}", execute=True, truncate_target=True)
    assert result.truncate is True
    assert result.inserted_counts["mail_messages"] == 1
