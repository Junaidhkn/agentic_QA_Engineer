"""Per-process X25519 key pair. Lease secrets are sealed to the public key and opened here only."""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SEAL_INFO = b"reliability-lease-secrets-v1"


class RunnerKeys:
    def __init__(self) -> None:
        self._private = X25519PrivateKey.generate()

    @property
    def public_b64(self) -> str:
        raw = self._private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def unseal(self, sealed_b64: str) -> dict[str, Any]:
        blob = base64.b64decode(sealed_b64)
        eph_pub, nonce, ct = blob[:32], blob[32:44], blob[44:]
        shared = self._private.exchange(X25519PublicKey.from_public_bytes(eph_pub))
        key = HKDF(hashes.SHA256(), 32, salt=eph_pub, info=SEAL_INFO).derive(shared)
        data: dict[str, Any] = json.loads(AESGCM(key).decrypt(nonce, ct, None))
        return data
