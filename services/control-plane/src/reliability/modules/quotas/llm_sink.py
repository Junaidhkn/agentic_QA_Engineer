"""Meters LLM calls for one organization: an ``llm_usage`` row per call and a committed
``llm_tokens`` ledger entry, and refuses calls once the monthly token quota is spent
(ADR-0019, FR-31). Each call uses its own short transaction so usage survives graph failures."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from reliability.llm.types import UsageRecord
from reliability.modules.quotas import service as quotas
from reliability.platform.db import tenant_tx
from reliability.platform.ids import uuid7


class LedgerUsageSink:
    async def allow(self, org_id: uuid.UUID, estimated_tokens: int) -> bool:
        async with tenant_tx(org_ids=[org_id]) as session:
            plan = await quotas.get_plan(session, org_id)
            used = await quotas.used(session, org_id, "llm_tokens")
            return used + estimated_tokens <= int(plan.monthly_llm_tokens)

    async def record(self, record: UsageRecord) -> None:
        async with tenant_tx(org_ids=[record.org_id]) as session:
            await session.execute(
                text(
                    "INSERT INTO llm_usage (id, org_id, purpose, subject_id, model, input_tokens, output_tokens,"
                    " cached_tokens, latency_ms, outcome) VALUES (:id, :org, :purpose, :subject, :model, :inp,"
                    " :out, :cached, :lat, :outcome)"
                ),
                {
                    "id": uuid7(),
                    "org": record.org_id,
                    "purpose": record.purpose,
                    "subject": record.subject_id,
                    "model": record.model,
                    "inp": record.input_tokens,
                    "out": record.output_tokens,
                    "cached": record.cached_tokens,
                    "lat": record.latency_ms,
                    "outcome": record.outcome,
                },
            )
            tokens = record.input_tokens + record.output_tokens
            if tokens:
                await quotas.record(session, record.org_id, "llm_tokens", tokens, record.subject_id)
