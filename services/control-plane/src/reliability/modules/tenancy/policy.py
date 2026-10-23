"""The single authorization policy layer (FR-30). Default deny.

Every endpoint states the minimum role (and, to allow API keys, the required scope).
Resources the caller cannot see raise NOT_FOUND rather than FORBIDDEN.
"""

from __future__ import annotations

import uuid

from reliability.modules.identity.service import Principal
from reliability.platform.errors import AppError
from reliability.platform.settings import get_settings

ROLE_ORDER = {"viewer": 0, "engineer": 1, "admin": 2, "owner": 3}


def role_at_least(role: str | None, minimum: str) -> bool:
    return role is not None and ROLE_ORDER[role] >= ROLE_ORDER[minimum]


def require(
    principal: Principal,
    org_id: uuid.UUID,
    minimum: str,
    *,
    scope: str | None = None,
    project_id: uuid.UUID | None = None,
) -> None:
    role = principal.org_roles.get(org_id)
    if role is None:
        raise AppError("NOT_FOUND", "resource not found")
    if principal.kind == "api_key":
        if scope is None or scope not in principal.scopes:
            raise AppError("FORBIDDEN", "API keys cannot perform this action", {"required_scope": scope})
        if project_id and principal.project_ids is not None and project_id not in principal.project_ids:
            raise AppError("NOT_FOUND", "resource not found")
        return
    if not role_at_least(role, minimum):
        raise AppError("FORBIDDEN", f"requires {minimum} role", {"role": role, "required": minimum})
    if minimum in ("admin", "owner") and not principal.mfa and get_settings().env in ("staging", "production"):
        raise AppError("FORBIDDEN", "multi-factor authentication is required for this action")


def default_org(principal: Principal, header_org: str | None) -> uuid.UUID:
    """Org for org-level creates: X-Org-Id header, or the caller's only org."""
    if header_org:
        try:
            org_id = uuid.UUID(header_org)
        except ValueError as exc:
            raise AppError("VALIDATION_FAILED", "invalid X-Org-Id") from exc
        if org_id not in principal.org_roles:
            raise AppError("NOT_FOUND", "organization not found")
        return org_id
    if len(principal.org_roles) == 1:
        return next(iter(principal.org_roles))
    raise AppError("VALIDATION_FAILED", "X-Org-Id header is required when you belong to several organizations")
