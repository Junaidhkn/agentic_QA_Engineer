from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from reliability.platform.db import Base


class Target(Base):
    __tablename__ = "targets"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    base_url: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(Text)
    environment: Mapped[str] = mapped_column(Text)
    verification_method: Mapped[str | None] = mapped_column(Text)
    verification_token: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resolved_ips: Mapped[list[str] | None] = mapped_column(ARRAY(INET))
    production_allowed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def is_runnable(self) -> bool:
        from datetime import UTC

        return (
            self.status == "verified"
            and self.verification_expires_at is not None
            and self.verification_expires_at > datetime.now(UTC)
        )
