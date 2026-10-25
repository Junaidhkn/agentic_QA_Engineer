"""Append-only, hash-chained audit log (FR-29, ADR-0023).

Each organization has its own chain (platform-level events use the ``platform`` chain and
must be written with a system session). ``hash = SHA-256(prev_hash || canonical_json(event))``;
modifying or deleting any row breaks every later hash and the daily anchor.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit.models import AuditAnchor, AuditEvent
from reliability.platform.actor import Actor
from reliability.platform.db import system_tx
from reliability.platform.storage import get_store

GENESIS = b"\x00" * 32


def _org_key(org_id: uuid.UUID | None) -> str:
    return str(org_id) if org_id else "platform"


def _canonical(event: dict[str, Any]) -> bytes:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode()


def compute_hash(prev_hash: bytes, event: dict[str, Any]) -> bytes:
    return hashlib.sha256(prev_hash + _canonical(event)).digest()


def _event_dict(row: AuditEvent) -> dict[str, Any]:
    return {
        "org_id": str(row.org_id) if row.org_id else None,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "action": row.action,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "metadata": row.metadata_ or {},
        "created_at": row.created_at.astimezone(UTC).isoformat(),
    }


async def record(
    session: AsyncSession,
    actor: Actor,
    action: str,
    *,
    org_id: uuid.UUID | None,
    subject_type: str | None = None,
    subject_id: uuid.UUID | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    key = _org_key(org_id)
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "audit:" + key})
    stmt = select(AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1)
    stmt = stmt.where(AuditEvent.org_id == org_id) if org_id else stmt.where(AuditEvent.org_id.is_(None))
    prev = (await session.execute(stmt)).scalar_one_or_none() or GENESIS
    row = AuditEvent(
        org_id=org_id,
        actor_type=actor.type,
        actor_id=actor.id,
        action=action,
        subject_type=subject_type,
        subject_id=str(subject_id) if subject_id else None,
        ip=actor.ip,
        user_agent=(actor.user_agent or "")[:300] or None,
        request_id=actor.request_id,
        metadata_=json.loads(json.dumps(metadata or {}, default=str)),
        created_at=datetime.now(UTC),
        prev_hash=prev,
    )
    row.hash = compute_hash(prev, _event_dict(row))
    session.add(row)
    await session.flush()
    return row


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    checked: int
    broken_at_seq: int | None = None


async def verify_chain(session: AsyncSession, org_id: uuid.UUID | None) -> ChainCheck:
    stmt = select(AuditEvent).order_by(AuditEvent.seq)
    stmt = stmt.where(AuditEvent.org_id == org_id) if org_id else stmt.where(AuditEvent.org_id.is_(None))
    prev = GENESIS
    count = 0
    for row in (await session.execute(stmt)).scalars():
        if row.prev_hash != prev or compute_hash(prev, _event_dict(row)) != row.hash:
            return ChainCheck(False, count, row.seq)
        prev = row.hash
        count += 1
    return ChainCheck(True, count)


async def anchor_all(today: date | None = None) -> int:
    """Write today's chain heads to ``audit_anchors`` and to (object-locked) storage."""
    today = today or datetime.now(UTC).date()
    written = 0
    async with system_tx() as session:
        heads = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT ON (coalesce(org_id::text, 'platform'))
                      coalesce(org_id::text, 'platform') AS org_key, seq, hash
                    FROM audit_events ORDER BY coalesce(org_id::text, 'platform'), seq DESC
                    """
                )
            )
        ).all()
        store = get_store()
        for head in heads:
            key = f"audit-anchors/{today.isoformat()}/{head.org_key}.json"
            store.put_bytes(
                key,
                json.dumps({"org": head.org_key, "seq": head.seq, "hash": head.hash.hex()}).encode(),
                "application/json",
            )
            existing = await session.get(AuditAnchor, (head.org_key, today))
            if existing is None:
                session.add(
                    AuditAnchor(
                        org_key=head.org_key, anchor_date=today, last_seq=head.seq, last_hash=head.hash, storage_key=key
                    )
                )
                written += 1
    return written
