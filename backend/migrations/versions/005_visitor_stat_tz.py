"""visitor_stats.last_active 从 naive TIMESTAMP 改为带时区的时间戳

Revision ID: 005_visitor_stat_tz
Revises: visitor_stats_v1
Create Date: 2026-09-30

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '005_visitor_stat_tz'
down_revision = 'visitor_stats_v1'
branch_labels = None
depends_on = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def upgrade():
    if _dialect() == 'sqlite':
        # SQLite 没有原生 timestamptz，datetime 以字符串存储且不强制时区，直接跳过
        return
    if _dialect() == 'postgresql':
        # 存量数据按 UTC 语义转换（历史值均为 datetime.utcnow 写入的 UTC naive 值）
        op.alter_column(
            'visitor_stats',
            'last_active',
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=True,
            existing_comment='最后活跃时间',
            postgresql_using="last_active AT TIME ZONE 'UTC'",
        )
        return
    op.alter_column(
        'visitor_stats',
        'last_active',
        existing_type=sa.DateTime(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        existing_comment='最后活跃时间',
    )


def downgrade():
    if _dialect() == 'sqlite':
        return
    if _dialect() == 'postgresql':
        op.alter_column(
            'visitor_stats',
            'last_active',
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            existing_nullable=True,
            existing_comment='最后活跃时间',
            postgresql_using="last_active AT TIME ZONE 'UTC'",
        )
        return
    op.alter_column(
        'visitor_stats',
        'last_active',
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(),
        existing_nullable=True,
        existing_comment='最后活跃时间',
    )
