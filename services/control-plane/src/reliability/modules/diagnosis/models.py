from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from reliability.platform.db import Base


class Diagnosis(Base):
    __tablename__ = "diagnoses"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    finding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text)
    agent_thread_id: Mapped[str | None] = mapped_column(Text)
    model_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    next_steps: Mapped[list[str] | None] = mapped_column(JSONB)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Hypothesis(Base):
    __tablename__ = "hypotheses"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    diagnosis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    rank: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(Text)
    statement: Mapped[str] = mapped_column(Text)
    mechanism: Mapped[str] = mapped_column(Text)
    fix_direction: Mapped[str | None] = mapped_column(Text)
    code_locations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    confidence: Mapped[str] = mapped_column(Text)
    confidence_factors: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    user_verdict: Mapped[str | None] = mapped_column(Text)
    user_verdict_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_verdict_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HypothesisEvidence(Base):
    __tablename__ = "hypothesis_evidence"
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    relation: Mapped[str] = mapped_column(Text)
    claim: Mapped[str] = mapped_column(Text)
    check_result: Mapped[str] = mapped_column(Text)
