from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from reliability.platform.db import Base


class LoadProfile(Base):
    __tablename__ = "load_profiles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    executor: Mapped[str] = mapped_column(Text)
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    max_vus: Mapped[int] = mapped_column(Integer)
    duration_s: Mapped[int] = mapped_column(Integer)
    think_time_min_s: Mapped[Decimal] = mapped_column(Numeric, default=1)
    think_time_max_s: Mapped[Decimal] = mapped_column(Numeric, default=3)
    workload_mix: Mapped[dict[str, float]] = mapped_column(JSONB)
    thresholds: Mapped[dict[str, float]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
