"""Plan features (threat-model A12): the free plan has neither browser exploration nor repair."""

from __future__ import annotations

import uuid

import httpx
import pytest

from reliability.modules.quotas import service as quotas
from reliability.platform.db import tenant_tx
from reliability.platform.errors import AppError
from tests.conftest import auth, make_org

pytestmark = pytest.mark.integration


async def test_free_plan_cannot_start_browser_exploration(api: httpx.AsyncClient) -> None:
    org = make_org("freebie", plan="free")
    r = await api.post(
        f"/v1/projects/{org.project_id}/discoveries",
        headers={**auth(org.email), "Idempotency-Key": str(uuid.uuid4())},
        json={"browser_exploration": {"target_id": str(uuid.uuid4())}},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "PLAN_LIMIT_EXCEEDED"
    assert r.json()["error"]["details"] == {"feature": "browser_exploration"}


async def test_plan_features_gate_repair() -> None:
    free, paid = make_org("freerep", plan="free"), make_org("paidrep", plan="indie")
    async with tenant_tx(org_ids=[free.org_id, paid.org_id]) as s:
        with pytest.raises(AppError) as exc:
            await quotas.require_feature(s, free.org_id, "repair", "Repair")
        assert exc.value.code == "PLAN_LIMIT_EXCEEDED" and "free plan" in exc.value.message
        await quotas.require_feature(s, paid.org_id, "repair", "Repair")
