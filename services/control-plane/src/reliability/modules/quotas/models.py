from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Date, DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from reliability.platform.db import Base


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    max_vus: Mapped[int] = mapped_column(Integer)
    max_duration_s: Mapped[int] = mapped_column(Integer)
    allowed_profile_types: Mapped[list[str]] = mapped_column(ARRAY(Text))
    monthly_vu_minutes: Mapped[int] = mapped_column(BigInteger)
    monthly_sandbox_minutes: Mapped[int] = mapped_column(BigInteger)
    monthly_llm_tokens: Mapped[int] = mapped_column(BigInteger)
    max_concurrent_runs: Mapped[int] = mapped_column(Integer)
    max_projects: Mapped[int] = mapped_column(Integer)
    artifact_retention_days: Mapped[int] = mapped_column(Integer)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB)


class UsageEntry(Base):
    __tablename__ = "usage_ledger"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, server_default=func.now())
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    period: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(BigInteger)
    entry_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
