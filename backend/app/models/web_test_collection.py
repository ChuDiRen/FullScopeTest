from datetime import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from ..database import Base



class WebTestCollection(Base):
    """Collection container for Web Playwright scripts."""

    __tablename__ = 'web_test_collections'

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=True, comment='Project ID')
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='User ID')
    name = Column(String(100), nullable=False, comment='Collection name')
    description = Column(Text, comment='Collection description')
    sort_order = Column(Integer, default=0, comment='Sort order')
    created_at = Column(DateTime, default=datetime.utcnow, comment='Created at')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='Updated at')

    scripts = relationship('WebTestScript', backref='collection', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'user_id': self.user_id,
            'name': self.name,
            'description': self.description,
            'sort_order': self.sort_order,
            'script_count': self.scripts.count(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<WebTestCollection {self.name}>'
