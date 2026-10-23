from __future__ import annotations

import time
import uuid

import pytest
from cryptography.exceptions import InvalidTag

from reliability.platform.crypto import LocalKms, seal, sign_token, unseal, verify_token
from reliability.platform.errors import AppError
from reliability.platform.storage import LocalObjectStore, object_key

KEY = "ZGV2LW9ubHktbWFzdGVyLWtleS0zMi1ieXRlcyEhISE="


def test_envelope_round_trip_and_context_binding() -> None:
    kms = LocalKms(KEY)
    sealed = seal("s3cr3t", {"org": "a", "name": "TOKEN"}, kms)
    assert unseal(sealed, {"org": "a", "name": "TOKEN"}, kms) == "s3cr3t"
    with pytest.raises(InvalidTag):
        unseal(sealed, {"org": "b", "name": "TOKEN"}, kms)


def test_tokens_are_audience_bound() -> None:
    token = sign_token({"sub": "run-1"}, "otlp", 60)
    assert verify_token(token, "otlp")["sub"] == "run-1"
    with pytest.raises(AppError):
        verify_token(token, "lease")


def test_object_keys_reject_traversal() -> None:
    org = uuid.uuid4()
    assert object_key(org, None, "k6_raw", "abc", "out.json").startswith(f"org/{org}/project/_org/")
    for bad in ("..", "a/b", "", "x" * 200):
        with pytest.raises(ValueError):
            object_key(org, None, "k6_raw", "abc", bad)


def test_local_store_presign_verification(tmp_path) -> None:
    store = LocalObjectStore(str(tmp_path), "http://api", "k")
    exp = int(time.time()) + 60
    sig = store.signature("put", "org/x/file", exp)
    assert store.verify("put", "org/x/file", exp, sig)
    assert not store.verify("put", "org/x/other", exp, sig)
    assert not store.verify("get", "org/x/file", exp, sig)
    assert not store.verify("put", "org/x/file", int(time.time()) - 1, store.signature("put", "org/x/file", 0))
    with pytest.raises(ValueError):
        store.put_bytes("../../etc/passwd", b"x")
