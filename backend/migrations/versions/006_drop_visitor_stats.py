"""下线访客统计功能：drop visitor_stats 表，清除存量 IP 等追踪数据

隐私合规收尾：该功能无告知收集访客 IP/设备/行为数据，并将访客 IP
外呼第三方（ip-api.com）查询归属地，整链路随本次清理一并移除
（后端 visitor_stats/geo 路由、模型、前端采集 hook 与隐藏统计页）。

Revision ID: 006_drop_visitor_stats
Revises: 005_visitor_stat_tz
Create Date: 2026-10-01

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '006_drop_visitor_stats'
down_revision = '005_visitor_stat_tz'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index('idx_visitor_ip_hash', table_name='visitor_stats')
    op.drop_index('idx_visitor_last_active', table_name='visitor_stats')
    op.drop_index('idx_visitor_first_visit', table_name='visitor_stats')
    op.drop_index('idx_visitor_session', table_name='visitor_stats')
    op.drop_table('visitor_stats')


def downgrade():
    """重建表结构（数据不可恢复——本迁移的目的就是清除追踪数据）"""
    op.create_table(
        'visitor_stats',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(length=64), nullable=False, comment='唯一会话标识'),
        sa.Column('ip_hash', sa.String(length=64), nullable=True, comment='IP 哈希值（隐私保护）'),
        sa.Column('ip_address', sa.String(length=45), nullable=True, comment='IP 地址（仅管理员可见）'),
        sa.Column('ip_country', sa.String(length=50), nullable=True, comment='IP 国家'),
        sa.Column('ip_city', sa.String(length=100), nullable=True, comment='IP 城市'),
        sa.Column('ip_isp', sa.String(length=100), nullable=True, comment='ISP 运营商'),
        sa.Column('first_visit', sa.DateTime(), nullable=True, comment='首次访问时间'),
        sa.Column('last_active', sa.DateTime(), nullable=True, comment='最后活跃时间'),
        sa.Column('total_duration', sa.Integer(), nullable=True, comment='总停留时长（秒）'),
        sa.Column('page_views', sa.Integer(), nullable=True, comment='页面浏览数'),
        sa.Column('device_type', sa.String(length=20), nullable=True, comment='设备类型'),
        sa.Column('browser', sa.String(length=50), nullable=True, comment='浏览器'),
        sa.Column('browser_version', sa.String(length=30), nullable=True, comment='浏览器版本'),
        sa.Column('os', sa.String(length=50), nullable=True, comment='操作系统'),
        sa.Column('os_version', sa.String(length=30), nullable=True, comment='操作系统版本'),
        sa.Column('screen_width', sa.Integer(), nullable=True, comment='屏幕宽度'),
        sa.Column('screen_height', sa.Integer(), nullable=True, comment='屏幕高度'),
        sa.Column('referrer', sa.Text(), nullable=True, comment='来源页面'),
        sa.Column('entry_page', sa.String(length=255), nullable=True, comment='入口页面'),
        sa.Column('visited_pages', sa.JSON(), nullable=True, comment='访问页面详情'),
        sa.Column('created_at', sa.DateTime(), nullable=True, comment='创建时间'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_id')
    )
    op.create_index('idx_visitor_session', 'visitor_stats', ['session_id'])
    op.create_index('idx_visitor_first_visit', 'visitor_stats', ['first_visit'])
    op.create_index('idx_visitor_last_active', 'visitor_stats', ['last_active'])
    op.create_index('idx_visitor_ip_hash', 'visitor_stats', ['ip_hash'])
