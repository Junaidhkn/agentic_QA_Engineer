from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from reliability.platform.db import Base


class RepairAttempt(Base):
    __tablename__ = "repair_attempts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    finding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    attempt_no: Mapped[int] = mapped_column(Integer)
    base_commit_sha: Mapped[str] = mapped_column(Text)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    status_reason: Mapped[str | None] = mapped_column(Text)
    patch_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    regression_test_paths: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    diff_stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    agent_summary: Mapped[str | None] = mapped_column(Text)
    agent_thread_id: Mapped[str | None] = mapped_column(Text, unique=True)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
