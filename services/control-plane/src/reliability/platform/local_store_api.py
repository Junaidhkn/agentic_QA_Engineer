"""Serves presigned URLs for the local object store (RE_OBJECT_STORE=local only)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from reliability.platform.errors import AppError
from reliability.platform.storage import LocalObjectStore, get_store

router = APIRouter(include_in_schema=False)
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def _store() -> LocalObjectStore:
    store = get_store()
    if not isinstance(store, LocalObjectStore):
        raise AppError("NOT_FOUND", "not found")
    return store


@router.get("/_local-store/{key:path}")
async def local_get(
    key: str, op: str, exp: int, sig: str, disposition: str = "attachment", filename: str | None = None
) -> Response:
    store = _store()
    extra = {"disposition": disposition}
    if filename:
        extra["filename"] = filename
    extra_s = "&".join(f"{k}={v}" for k, v in sorted(extra.items()))
    if op != "get" or not store.verify("get", key, exp, sig, extra_s) or not store.exists(key):
        raise AppError("NOT_FOUND", "not found")
    header = disposition if disposition == "inline" else "attachment"
    if filename:
        header += f'; filename="{filename}"'
    return Response(
        store.get_bytes(key), media_type="application/octet-stream", headers={"Content-Disposition": header}
    )


@router.put("/_local-store/{key:path}", status_code=204)
async def local_put(key: str, op: str, exp: int, sig: str, request: Request) -> Response:
    store = _store()
    if op != "put" or not store.verify("put", key, exp, sig):
        raise AppError("FORBIDDEN", "invalid signature")
    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        raise AppError("VALIDATION_FAILED", "upload too large")
    store.put_bytes(key, body)
    return Response(status_code=204)
