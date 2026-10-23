"""Registers every module's API router under ``/v1``. Extended as modules are added."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from reliability.modules.approvals import api as approvals_api
from reliability.modules.artifacts import api as artifacts_api
from reliability.modules.identity import api as identity_api
from reliability.modules.integrations.github import api as github_api
from reliability.modules.loadprofiles import api as loadprofiles_api
from reliability.modules.projects import api as projects_api
from reliability.modules.runners import api as runners_api
from reliability.modules.runs import api as runs_api
from reliability.modules.secrets import api as secrets_api
from reliability.modules.targets import api as targets_api
from reliability.modules.tenancy import api as tenancy_api
from reliability.modules.testsuites import api as testsuites_api
from reliability.modules.workflows import api as workflows_api
from reliability.platform import local_store_api
from reliability.platform.settings import get_settings


def include_routers(app: FastAPI) -> None:
    v1 = APIRouter(prefix="/v1")
    if get_settings().object_store == "local":
        v1.include_router(local_store_api.router)
    for module in (
        identity_api,
        tenancy_api,
        projects_api,
        targets_api,
        secrets_api,
        github_api,
        workflows_api,
        testsuites_api,
        loadprofiles_api,
        runs_api,
        approvals_api,
        artifacts_api,
    ):
        v1.include_router(module.router)
    app.include_router(v1)
    app.include_router(github_api.webhook_router)
    app.include_router(runners_api.router)
