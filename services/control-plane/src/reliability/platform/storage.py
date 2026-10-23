"""Object storage adapter (ADR-0005).

Keys are always built by :func:`object_key` — never from user input — and are prefixed with
the organization id. Browsers and runners only ever receive short-lived presigned URLs.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

from reliability.platform.settings import get_settings

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def object_key(
    org_id: uuid.UUID, project_id: uuid.UUID | None, kind: str, object_id: uuid.UUID | str, name: str
) -> str:
    for seg in (kind, str(object_id), name):
        if not _SAFE_SEGMENT.match(seg) or seg in (".", ".."):
            raise ValueError(f"unsafe key segment: {seg!r}")
    project = str(project_id) if project_id else "_org"
    return f"org/{org_id}/project/{project}/{kind}/{object_id}/{name}"


class ObjectStore(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def presign_get(
        self, key: str, ttl: int | None = None, filename: str | None = None, inline: bool = False
    ) -> str: ...
    def presign_put(self, key: str, ttl: int | None = None, content_type: str = "application/octet-stream") -> str: ...


class LocalObjectStore:
    """Filesystem store for development and tests; presigned URLs are HMAC-signed API routes."""

    def __init__(self, root: str, base_url: str, signing_key: str) -> None:
        self.root = Path(root).resolve()
        self.base_url = base_url.rstrip("/")
        self._key = signing_key.encode()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("key escapes store root")
        return path

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix.rstrip("/"))
        if not base.exists():
            return 0
        count = 0
        for file in sorted(base.rglob("*"), reverse=True):
            if file.is_file():
                file.unlink()
                count += 1
            else:
                file.rmdir()
        if base.is_dir():
            base.rmdir()
        return count

    def signature(self, op: str, key: str, exp: int, extra: str = "") -> str:
        msg = f"{op}\n{key}\n{exp}\n{extra}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def verify(self, op: str, key: str, exp: int, sig: str, extra: str = "") -> bool:
        return exp >= int(time.time()) and hmac.compare_digest(self.signature(op, key, exp, extra), sig)

    def _url(self, op: str, key: str, ttl: int, extra: dict[str, str]) -> str:
        exp = int(time.time()) + ttl
        extra_s = "&".join(f"{k}={v}" for k, v in sorted(extra.items()))
        params = {"op": op, "exp": str(exp), **extra, "sig": self.signature(op, key, exp, extra_s)}
        return f"{self.base_url}/v1/_local-store/{quote(key)}?{urlencode(params)}"

    def presign_get(self, key: str, ttl: int | None = None, filename: str | None = None, inline: bool = False) -> str:
        extra = {"disposition": "inline" if inline else "attachment"}
        if filename:
            extra["filename"] = filename
        return self._url("get", key, ttl or get_settings().presign_ttl_seconds, extra)

    def presign_put(self, key: str, ttl: int | None = None, content_type: str = "application/octet-stream") -> str:
        return self._url("put", key, ttl or get_settings().presign_ttl_seconds, {})


class S3ObjectStore:
    def __init__(self, bucket: str, region: str, endpoint_url: str | None) -> None:
        import boto3

        self.bucket = bucket
        self._s3 = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._s3.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, ServerSideEncryption="aws:kms"
        )

    def get_bytes(self, key: str) -> bytes:
        body: bytes = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return body

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except self._s3.exceptions.ClientError:
            return False

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=key)

    def delete_prefix(self, prefix: str) -> int:
        count = 0
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                self._s3.delete_objects(Bucket=self.bucket, Delete={"Objects": objs})
                count += len(objs)
        return count

    def presign_get(self, key: str, ttl: int | None = None, filename: str | None = None, inline: bool = False) -> str:
        params = {"Bucket": self.bucket, "Key": key}
        disposition = "inline" if inline else "attachment"
        if filename:
            disposition += f'; filename="{filename}"'
        params["ResponseContentDisposition"] = disposition
        url: str = self._s3.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=ttl or get_settings().presign_ttl_seconds
        )
        return url

    def presign_put(self, key: str, ttl: int | None = None, content_type: str = "application/octet-stream") -> str:
        url: str = self._s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=ttl or get_settings().presign_ttl_seconds,
        )
        return url


_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    global _store
    if _store is None:
        s = get_settings()
        if s.object_store == "s3":
            _store = S3ObjectStore(s.s3_bucket, s.s3_region, s.s3_endpoint_url)
        else:
            _store = LocalObjectStore(s.local_store_path, s.api_base_url, s.token_signing_key.get_secret_value())
    return _store


def reset_store() -> None:
    global _store
    _store = None
