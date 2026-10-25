from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, Numeric, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from reliability.platform.db import Base


class Verification(Base):
    __tablename__ = "verifications"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    repair_attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    status: Mapped[str] = mapped_column(Text)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    archives: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationGate(Base):
    __tablename__ = "verification_gates"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    repair_attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    gate: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    applicable: Mapped[bool] = mapped_column(Boolean)
    run_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PerformanceComparison(Base):
    __tablename__ = "performance_comparisons"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    repair_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    baseline_run_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    candidate_run_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    scope: Mapped[str] = mapped_column(Text)
    metric: Mapped[str] = mapped_column(Text)
    baseline_value: Mapped[Decimal] = mapped_column(Numeric)
    candidate_value: Mapped[Decimal] = mapped_column(Numeric)
    delta_pct: Mapped[Decimal] = mapped_column(Numeric)
    ci_low_pct: Mapped[Decimal] = mapped_column(Numeric)
    ci_high_pct: Mapped[Decimal] = mapped_column(Numeric)
    noise_floor_pct: Mapped[Decimal] = mapped_column(Numeric)
    verdict: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    environment_controlled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
