"""Versioned test suites compiled from approved workflows (FR-10)."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.artifacts import service as artifacts
from reliability.modules.audit import service as audit
from reliability.modules.identity.service import Principal
from reliability.modules.projects.models import Project
from reliability.modules.testsuites.compiler import RUNTIME_VERSION, compile_bundle
from reliability.modules.testsuites.models import TestCase, TestSuite
from reliability.modules.workflows.ir import api_steps
from reliability.modules.workflows.models import Workflow
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

BUNDLE_PARTS = ("k6", "playwright")


async def generate(
    session: AsyncSession,
    principal: Principal,
    project: Project,
    workflow_ids: list[uuid.UUID] | None = None,
    commit_sha: str | None = None,
    generator_info: dict[str, Any] | None = None,
) -> TestSuite:
    stmt = select(Workflow).where(Workflow.project_id == project.id, Workflow.status == "approved")
    if workflow_ids:
        stmt = stmt.where(Workflow.id.in_(workflow_ids))
    workflows = list((await session.execute(stmt)).scalars())
    if not workflows:
        raise AppError("CONFLICT", "no approved workflows to generate tests from")
    bundle = compile_bundle([w.spec for w in workflows])
    version = (
        int(
            await session.scalar(
                select(func.coalesce(func.max(TestSuite.version), 0)).where(TestSuite.project_id == project.id)
            )
            or 0
        )
        + 1
    )
    suite_id = uuid7()
    prefix = artifacts.new_key(project.org_id, project.id, "test_bundle", suite_id, "x").rsplit("/", 1)[0]
    suite = TestSuite(
        id=suite_id,
        org_id=project.org_id,
        project_id=project.id,
        version=version,
        status="ready",
        commit_sha=commit_sha,
        artifact_prefix=prefix,
        created_by=principal.user_id,
        generator_info={
            "compiler": "deterministic-ir",
            "runtime_version": RUNTIME_VERSION,
            "ir_hashes": {
                w.key: hashlib.sha256(json.dumps(w.spec, sort_keys=True).encode()).hexdigest()[:16] for w in workflows
            },
            **(generator_info or {}),
        },
    )
    session.add(suite)
    await session.flush()
    for part in BUNDLE_PARTS:
        await artifacts.put(
            session,
            org_id=project.org_id,
            project_id=project.id,
            kind="test_bundle",
            owner_id=suite_id,
            name=f"{part}.tar.gz",
            data=bundle.tarball(part),
            content_type="application/gzip",
        )
    await artifacts.put(
        session,
        org_id=project.org_id,
        project_id=project.id,
        kind="test_bundle",
        owner_id=suite_id,
        name="manifest.json",
        data=bundle.files["manifest.json"].encode(),
        content_type="application/json",
    )
    by_key = {w.key: w for w in workflows}
    for case in bundle.cases:
        content = bundle.files[case.path].encode()
        session.add(
            TestCase(
                id=uuid7(),
                org_id=project.org_id,
                suite_id=suite_id,
                workflow_id=by_key[case.workflow_key].id,
                type=case.type if case.type != "k6_probe" else "k6_probe",
                path=case.path,
                artifact_key=f"{prefix}/{case.path.split('/', 1)[0]}.tar.gz",
                sha256=hashlib.sha256(content).digest(),
                validation_status="pending",
                validation_detail={"invariant_id": case.invariant_id} if case.invariant_id else None,
            )
        )
    await session.flush()
    await audit.record(
        session,
        principal.actor,
        "test_suite.generated",
        org_id=project.org_id,
        subject_type="test_suite",
        subject_id=suite_id,
        metadata={"version": version, "workflows": len(workflows), "cases": len(bundle.cases)},
    )
    return suite


async def get(session: AsyncSession, suite_id: uuid.UUID) -> TestSuite:
    suite = await session.get(TestSuite, suite_id)
    if suite is None:
        raise AppError("NOT_FOUND", "test suite not found")
    return suite


async def latest_ready(session: AsyncSession, project_id: uuid.UUID) -> TestSuite | None:
    return (
        await session.execute(
            select(TestSuite)
            .where(TestSuite.project_id == project_id, TestSuite.status.in_(("ready", "partially_ready")))
            .order_by(TestSuite.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_for(session: AsyncSession, project_id: uuid.UUID) -> list[TestSuite]:
    return list(
        (
            await session.execute(
                select(TestSuite).where(TestSuite.project_id == project_id).order_by(TestSuite.version.desc())
            )
        ).scalars()
    )


async def cases(session: AsyncSession, suite_id: uuid.UUID) -> list[tuple[TestCase, Workflow]]:
    rows = await session.execute(
        select(TestCase, Workflow)
        .join(Workflow, Workflow.id == TestCase.workflow_id)
        .where(TestCase.suite_id == suite_id)
        .order_by(Workflow.key, TestCase.type)
    )
    return [(c, w) for c, w in rows.all()]


async def runnable(session: AsyncSession, suite: TestSuite) -> dict[str, Any]:
    """What a run can execute from this suite: k6 exec names + endpoints per workflow, browser specs,
    probes — excluding cases whose validation failed (FR-12)."""
    manifest = json.loads(artifacts.read(await _manifest(session, suite)))
    k6: dict[str, str] = {}
    endpoints: dict[str, list[str]] = {}
    playwright: list[str] = []
    probes: list[dict[str, str]] = []
    secret_names: set[str] = set()
    from reliability.modules.workflows.ir import secret_names as ir_secrets

    for case, wf in await cases(session, suite.id):
        if case.validation_status == "invalid":
            continue
        secret_names |= ir_secrets(wf.spec)
        if case.type == "k6_scenario":
            k6[wf.key] = manifest["exec_names"][wf.key]
            endpoints[wf.key] = sorted({f"{s['request']['method']} {s['request']['path']}" for s in api_steps(wf.spec)})
        elif case.type == "playwright":
            playwright.append(case.path.split("/", 1)[1])
        elif case.type == "k6_probe":
            probes.append(
                {
                    "workflow": wf.key,
                    "path": case.path.split("/", 1)[1],
                    "invariant_id": (case.validation_detail or {}).get("invariant_id", ""),
                }
            )
    return {
        "exec_names": k6,
        "endpoints": endpoints,
        "playwright_specs": playwright,
        "probes": probes,
        "secret_names": sorted(secret_names),
    }


async def _manifest(session: AsyncSession, suite: TestSuite) -> artifacts.Artifact:
    key = f"{suite.artifact_prefix}/manifest.json"
    art = (
        await session.execute(select(artifacts.Artifact).where(artifacts.Artifact.storage_key == key))
    ).scalar_one_or_none()
    if art is None:
        raise AppError("NOT_FOUND", "suite manifest missing")
    return art


async def bundle_artifact(session: AsyncSession, suite: TestSuite, part: str) -> artifacts.Artifact:
    if part not in BUNDLE_PARTS:
        raise AppError("VALIDATION_FAILED", "part must be k6 or playwright")
    key = f"{suite.artifact_prefix}/{part}.tar.gz"
    art = (
        await session.execute(select(artifacts.Artifact).where(artifacts.Artifact.storage_key == key))
    ).scalar_one_or_none()
    if art is None:
        raise AppError("NOT_FOUND", "bundle not found")
    return art
