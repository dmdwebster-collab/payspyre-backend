"""``platform_application_process_config`` — single-row application-process config
(Wave 2 W2-APPCONFIG; TL videos 07-08 "Application process").

One row (id forced to 1) holding the whole typed
:class:`app.schemas.application_process_config.ApplicationProcessConfig` document
in a JSONB ``config`` blob. No row → shipped defaults (current behaviour); the
service (:mod:`app.services.application_process_config`) upserts row 1 and never
reads this table directly elsewhere.
"""
from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class PlatformApplicationProcessConfig(Base):
    __tablename__ = "platform_application_process_config"

    id = Column(Integer, primary_key=True, default=1)
    config = Column(JSONB, nullable=False, default=dict)
    updated_by = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_platform_application_process_config_single_row"),
    )
