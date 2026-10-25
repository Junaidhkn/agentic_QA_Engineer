"""Phase 1: tenancy (RLS), outbox, audit chain, quotas, idempotency, rate limits."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from reliability.modules.audit import service as audit
from reliability.modules.quotas import ratelimit
from reliability.modules.quotas import service as quotas
from reliability.platform import idempotency, outbox
from reliability.platform.actor import Actor
from reliability.platform.db import system_tx, tenant_tx
from reliability.platform.errors import AppError
from tests.conftest import OrgFixture, make_org, sql

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------ RLS ----
def test_every_tenant_table_has_forced_rls() -> None:
    rows = sql(
        """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
          AND c.oid NOT IN (SELECT inhrelid FROM pg_inherits)
          AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'org_id')
        """
    )
    system_only = {"outbox", "abuse_reviews"}
    missing = [r[0] for r in rows if r[0] not in system_only and not (r[1] and r[2])]
    assert rows and not missing, f"tables without forced RLS: {missing}"


async def test_tenant_cannot_read_or_write_other_tenant(org_a: OrgFixture, org_b: OrgFixture) -> None:
    async with tenant_tx(user_id=org_a.user_id, org_ids=[org_a.org_id]) as s:
        names = (await s.execute(text("SELECT org_id FROM projects"))).scalars().all()
        assert set(names) == {org_a.org_id}
        orgs = (await s.execute(text("SELECT id FROM organizations"))).scalars().all()
        assert orgs == [org_a.org_id]

    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_tx(user_id=org_a.user_id, org_ids=[org_a.org_id]) as s:
            await s.execute(
                text("INSERT INTO projects (id, org_id, name) VALUES (:i, :o, 'evil')"),
                {"i": uuid.uuid4(), "o": org_b.org_id},
            )


async def test_no_tenant_context_sees_nothing(org_a: OrgFixture) -> None:
    async with tenant_tx() as s:
        assert (await s.execute(text("SELECT count(*) FROM projects"))).scalar_one() == 0


async def test_memberships_visible_by_user_before_org_context(org_a: OrgFixture) -> None:
    async with tenant_tx(user_id=org_a.user_id) as s:
        rows = (await s.execute(text("SELECT org_id, role FROM memberships"))).all()
    assert [(r.org_id, r.role) for r in rows] == [(org_a.org_id, "owner")]


async def test_app_role_cannot_update_audit_events(org_a: OrgFixture) -> None:
    async with tenant_tx(org_ids=[org_a.org_id]) as s:
        await audit.record(s, Actor("user", str(org_a.user_id)), "test.event", org_id=org_a.org_id)
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_tx(org_ids=[org_a.org_id]) as s:
            await s.execute(text("UPDATE audit_events SET action = 'x'"))


# --------------------------------------------------------------- outbox ----
async def test_outbox_rollback_publishes_nothing_and_commit_publishes_once(org_a: OrgFixture) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with tenant_tx(org_ids=[org_a.org_id]) as s:
            await outbox.enqueue(s, "test.topic", {"n": 1}, org_id=org_a.org_id)
            raise Boom

    async with tenant_tx(org_ids=[org_a.org_id]) as s:
        await outbox.enqueue(s, "test.topic", {"n": 2}, org_id=org_a.org_id)

    published: list[tuple[str, dict]] = []
    assert await outbox.relay_once(lambda t, p: published.append((t, p))) == 1
    assert await outbox.relay_once(lambda t, p: published.append((t, p))) == 0
    assert published == [("test.topic", {"n": 2})]


async def test_outbox_publish_failure_is_retried_later(org_a: OrgFixture) -> None:
    async with tenant_tx(org_ids=[org_a.org_id]) as s:
        await outbox.enqueue(s, "test.topic", {"n": 3})

    def broken(topic: str, payload: dict) -> None:
        raise ConnectionError("broker down")

    assert await outbox.relay_once(broken) == 0
    attempts, dispatched = sql("SELECT attempts, dispatched_at FROM outbox")[0]
    assert attempts == 1 and dispatched is None


# ---------------------------------------------------------------- audit ----
async def test_audit_chain_verifies_and_detects_tampering(org_a: OrgFixture) -> None:
    actor = Actor("user", str(org_a.user_id), ip="203.0.113.5", request_id="req_1")
    for i in range(3):
        async with tenant_tx(org_ids=[org_a.org_id]) as s:
            await audit.record(
                s, actor, f"thing.{i}", org_id=org_a.org_id, subject_type="thing", subject_id=str(i), metadata={"i": i}
            )
    async with tenant_tx(org_ids=[org_a.org_id]) as s:
        check = await audit.verify_chain(s, org_a.org_id)
    assert check.ok and check.checked == 3

    sql("UPDATE audit_events SET metadata = '{\"i\": 99}' WHERE action = 'thing.1'")
    async with tenant_tx(org_ids=[org_a.org_id]) as s:
        check = await audit.verify_chain(s, org_a.org_id)
    assert not check.ok and check.broken_at_seq is not None


async def test_audit_anchor_writes_heads(org_a: OrgFixture) -> None:
    async with tenant_tx(org_ids=[org_a.org_id]) as s:
        await audit.record(s, Actor("system", "system"), "x", org_id=org_a.org_id)
    assert await audit.anchor_all() == 1
    assert sql("SELECT count(*) FROM audit_anchors")[0][0] == 1


# --------------------------------------------------------------- quotas ----
async def test_quota_reservations_never_exceed_limit_under_concurrency() -> None:
    org = make_org("quota", plan="free")  # 500 VU-minutes per month

    async def attempt() -> bool:
        try:
            async with tenant_tx(org_ids=[org.org_id]) as s:
                await quotas.reserve(s, org.org_id, "vu_minutes", 100)
            return True
        except AppError as exc:
            assert exc.code == "QUOTA_EXCEEDED"
            return False

    results = await asyncio.gather(*(attempt() for _ in range(8)))
    assert sum(results) == 5
    async with tenant_tx(org_ids=[org.org_id]) as s:
        assert await quotas.used(s, org.org_id, "vu_minutes") == 500


async def test_quota_settle_replaces_reservation(org_a: OrgFixture) -> None:
    async with tenant_tx(org_ids=[org_a.org_id]) as s:
        await quotas.reserve(s, org_a.org_id, "vu_minutes", 1000)
        await quotas.settle(s, org_a.org_id, "vu_minutes", reserved=1000, actual=250)
        assert await quotas.used(s, org_a.org_id, "vu_minutes") == 250
        summary = await quotas.usage_summary(s, org_a.org_id)
    assert summary["vu_minutes"] == {"used": 250, "limit": 100000}


# ---------------------------------------------------------- idempotency ----
async def test_idempotency_replay_and_conflict(org_a: OrgFixture) -> None:
    async with tenant_tx(org_ids=[org_a.org_id]) as s:
        assert await idempotency.lookup(s, org_a.org_id, "key-12345", {"a": 1}) is None
        await idempotency.store(s, org_a.org_id, "key-12345", {"a": 1}, 202, {"id": "x"})
    async with tenant_tx(org_ids=[org_a.org_id]) as s:
        cached = await idempotency.lookup(s, org_a.org_id, "key-12345", {"a": 1})
        assert cached is not None and cached.status_code == 202 and cached.response == {"id": "x"}
        with pytest.raises(AppError) as exc:
            await idempotency.lookup(s, org_a.org_id, "key-12345", {"a": 2})
        assert exc.value.code == "IDEMPOTENCY_CONFLICT"


# ----------------------------------------------------------- rate limit ----
async def test_rate_limit_exhausts_bucket() -> None:
    for _ in range(ratelimit.CLASSES["verify"].capacity):
        await ratelimit.check("p1", "verify")
    with pytest.raises(AppError) as exc:
        await ratelimit.check("p1", "verify")
    assert exc.value.code == "RATE_LIMITED" and int(exc.value.headers["Retry-After"]) >= 1
    await ratelimit.check("p2", "verify")  # other principals unaffected


async def test_system_tx_bypasses_rls(org_a: OrgFixture, org_b: OrgFixture) -> None:
    async with system_tx() as s:
        assert (await s.execute(text("SELECT count(*) FROM projects"))).scalar_one() == 2


async def test_partition_maintenance_creates_future_months() -> None:
    from datetime import date

    from reliability.platform.maintenance import ensure_partitions

    created = await ensure_partitions(2, today=date(2030, 1, 15))
    assert "run_events_203002" in created and "audit_events_203003" in created
    assert await ensure_partitions(2, today=date(2030, 1, 15)) == []
