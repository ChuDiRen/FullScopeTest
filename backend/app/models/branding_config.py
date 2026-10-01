"""
品牌配置模型

支持企业客户自定义品牌外观：
- 平台名称
- Logo URL
- Favicon URL
- 主色调
- 登录页背景图
- Footer 文案
"""
from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from ..database import Base


class BrandingConfig(Base):
    """品牌配置表"""
    __tablename__ = 'branding_configs'

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, comment='组织 ID（NULL 为全局默认）')
    platform_name = Column(String(100), default='大熊AI测试平台', comment='平台名称')
    logo_url = Column(String(500), comment='Logo URL')
    favicon_url = Column(String(500), comment='Favicon URL')
    primary_color = Column(String(20), default='#5FA59B', comment='主色调')
    login_background_url = Column(String(500), comment='登录页背景图 URL')
    footer_text = Column(String(200), comment='Footer 文案')
    custom_css = Column(Text, comment='自定义 CSS')

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    organization = relationship('Organization', backref='branding_config')

    def to_dict(self):
        return {
            'id': self.id,
            'organization_id': self.organization_id,
            'platform_name': self.platform_name,
            'logo_url': self.logo_url,
            'favicon_url': self.favicon_url,
            'primary_color': self.primary_color,
            'login_background_url': self.login_background_url,
            'footer_text': self.footer_text,
            'custom_css': self.custom_css,
        }
