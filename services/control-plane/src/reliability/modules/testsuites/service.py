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
from reliability.modules.targets.models import Target
from reliability.modules.testsuites.compiler import RUNTIME_VERSION, compile_bundle
from reliability.modules.testsuites.models import TestCase, TestSuite
from reliability.modules.workflows.ir import api_steps
from reliability.modules.workflows.models import Workflow
from reliability.platform import outbox
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
    validate_target_id: uuid.UUID | None = None,
) -> TestSuite:
    """Create a suite version from approved workflows. Without a validation target the bundle
    is compiled immediately (cases ``pending``); with one, the test-generation graph compiles,
    dry-runs (3×) and repairs the suite in the background (status ``generating`` → ``validating``
    → ``ready``/``partially_ready``/``failed``)."""
    stmt = select(Workflow).where(Workflow.project_id == project.id, Workflow.status == "approved")
    if workflow_ids:
        stmt = stmt.where(Workflow.id.in_(workflow_ids))
    workflows = list((await session.execute(stmt)).scalars())
    if not workflows:
        raise AppError("CONFLICT", "no approved workflows to generate tests from")
    target = await validation_target(session, project, validate_target_id) if validate_target_id else None
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
    info: dict[str, Any] = {
        "compiler": "deterministic-ir",
        "runtime_version": RUNTIME_VERSION,
        **(generator_info or {}),
    }
    if target is not None:
        info.update(validation_target_id=str(target.id), workflow_ids=[str(w.id) for w in workflows])
    suite = TestSuite(
        id=suite_id,
        org_id=project.org_id,
        project_id=project.id,
        version=version,
        status="generating" if target else "ready",
        commit_sha=commit_sha,
        artifact_prefix=prefix,
        agent_thread_id=f"testgen:{suite_id}" if target else None,
        created_by=principal.user_id,
        generator_info=info,
    )
    session.add(suite)
    await session.flush()
    if target is not None:
        from reliability.modules.discovery.models import AgentThread

        session.add(
            AgentThread(thread_id=suite.agent_thread_id, org_id=project.org_id, graph="testgen", subject_id=suite_id)
        )
        await outbox.enqueue(
            session, "testsuites.generate", {"suite_id": suite_id, "org_id": project.org_id}, org_id=project.org_id
        )
    else:
        await write_bundle(session, suite, workflows, {w.key: w.spec for w in workflows})
    await audit.record(
        session,
        principal.actor,
        "test_suite.generated",
        org_id=project.org_id,
        subject_type="test_suite",
        subject_id=suite_id,
        metadata={"version": version, "workflows": len(workflows), "validate": bool(target)},
    )
    return suite


async def validation_target(session: AsyncSession, project: Project, target_id: uuid.UUID) -> Target:
    """Dry runs send a handful of requests (1 VU, 1 iteration, 3 repeats): the target must be
    verified and its environment policy must allow smoke traffic."""
    from reliability.modules.targets import service as targets

    target = await session.get(Target, target_id)
    if target is None or target.project_id != project.id:
        raise AppError("NOT_FOUND", "target not found")
    if not target.is_runnable:
        raise AppError("TARGET_NOT_VERIFIED", f"target {target.host} is not verified", {"target_id": str(target.id)})
    violation = targets.policy_violation(target, "smoke", 1, 600)
    if violation:
        raise AppError("ENVIRONMENT_POLICY_VIOLATION", violation, {"environment": target.environment})
    return target


async def write_bundle(
    session: AsyncSession, suite: TestSuite, workflows: list[Workflow], irs: dict[str, dict[str, Any]]
) -> list[TestCase]:
    """Compile ``irs`` (workflow key → IR, possibly repaired) into the suite's bundle, replacing
    a previous bundle of the same suite, and upsert one test case per output."""
    bundle = compile_bundle([irs[w.key] for w in workflows])
    suite.generator_info = {
        **suite.generator_info,
        "ir_hashes": {
            k: hashlib.sha256(json.dumps(ir, sort_keys=True).encode()).hexdigest()[:16] for k, ir in irs.items()
        },
    }
    files = {f"{part}.tar.gz": (bundle.tarball(part), "application/gzip") for part in BUNDLE_PARTS}
    files["manifest.json"] = (bundle.files["manifest.json"].encode(), "application/json")
    for name, (data, content_type) in files.items():
        existing = (
            await session.execute(
                select(artifacts.Artifact).where(artifacts.Artifact.storage_key == f"{suite.artifact_prefix}/{name}")
            )
        ).scalar_one_or_none()
        if existing is not None:
            await artifacts.replace(session, existing, data, content_type)
        else:
            await artifacts.put(
                session,
                org_id=suite.org_id,
                project_id=suite.project_id,
                kind="test_bundle",
                owner_id=suite.id,
                name=name,
                data=data,
                content_type=content_type,
            )
    by_key = {w.key: w for w in workflows}
    existing_cases = {
        (c.workflow_id, c.type): c
        for c in (await session.execute(select(TestCase).where(TestCase.suite_id == suite.id))).scalars()
    }
    out: list[TestCase] = []
    for case in bundle.cases:
        content = bundle.files[case.path].encode()
        wf = by_key[case.workflow_key]
        digest = hashlib.sha256(content).digest()
        tc = existing_cases.get((wf.id, case.type))
        if tc is None:
            tc = TestCase(
                id=uuid7(),
                org_id=suite.org_id,
                suite_id=suite.id,
                workflow_id=wf.id,
                type=case.type,
                path=case.path,
                artifact_key=f"{suite.artifact_prefix}/{case.path.split('/', 1)[0]}.tar.gz",
                sha256=digest,
                validation_status="pending",
                attempts=0,
                validation_detail={"invariant_id": case.invariant_id} if case.invariant_id else None,
            )
            session.add(tc)
        elif tc.sha256 != digest:
            tc.path, tc.sha256 = case.path, digest
            tc.attempts = min(4, tc.attempts + 1)
        out.append(tc)
    await session.flush()
    return out


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
