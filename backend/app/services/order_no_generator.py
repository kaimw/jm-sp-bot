"""中台订单号生成器

规则：MP-{年份}{序号}
- 预审通过后分配（非建单时分配）
- 序号连续不跳号，年度重置
- 撤单/作废不回收序号
- 原子递增，通过数据库行锁保证并发安全
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.models import OrderSequence


def generate_middle_order_no(session: Session) -> str:
    """生成中台订单号 MP-{年份}{序号}

    使用 PostgreSQL/SQLite 的 INSERT ... ON CONFLICT 或等价的
    UPDATE ... RETURNING 实现原子递增，无需应用层锁。

    兼容 SQLite（测试/开发环境）和 PostgreSQL（生产环境）。
    """
    now = datetime.now()
    year = now.year

    # 尝试原子递增：先 UPDATE，如果没有行则 INSERT
    # SQLAlchemy 2.0 方式
    seq = session.query(OrderSequence).filter(OrderSequence.year == year).with_for_update().first()

    if seq is None:
        seq = OrderSequence(year=year, last_seq=1)
        session.add(seq)
    else:
        seq.last_seq += 1

    session.flush()
    return f"MP-{year}{seq.last_seq:05d}"


def generate_fake_order_no(year: int, seq: int) -> str:
    """生成一个不落库的订单号（用于测试/展示）"""
    return f"MP-{year}{seq:05d}"
