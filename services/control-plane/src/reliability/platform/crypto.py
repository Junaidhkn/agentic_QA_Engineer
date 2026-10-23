"""Hashing, envelope encryption and signed tokens.

* API keys, invitation and share tokens are stored as HMAC-SHA256 with a server pepper.
* Project secrets use envelope encryption: a fresh 256-bit data key per secret (AES-GCM),
  wrapped by KMS (AWS) or by a local master key (dev/test).
* Short-lived scoped tokens (run/OTLP/lease/share) are HS256 JWTs with explicit audience.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from reliability.platform.errors import AppError
from reliability.platform.settings import get_settings


def hmac_digest(value: str, key: str | None = None) -> bytes:
    pepper = key or get_settings().api_key_pepper.get_secret_value()
    return hmac.new(pepper.encode(), value.encode(), hashlib.sha256).digest()


def constant_time_equals(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# ------------------------------------------------------------------ KMS ----
class Kms(Protocol):
    def generate_data_key(self, context: dict[str, str]) -> tuple[bytes, bytes]: ...
    def decrypt_data_key(self, wrapped: bytes, context: dict[str, str]) -> bytes: ...


class LocalKms:
    """AES-GCM key wrapping with a local master key. Development and tests only."""

    def __init__(self, master_key_b64: str) -> None:
        key = base64.b64decode(master_key_b64)
        if len(key) != 32:
            raise ValueError("RE_LOCAL_MASTER_KEY must be 32 bytes base64")
        self._aead = AESGCM(key)

    @staticmethod
    def _aad(context: dict[str, str]) -> bytes:
        return "|".join(f"{k}={context[k]}" for k in sorted(context)).encode()

    def generate_data_key(self, context: dict[str, str]) -> tuple[bytes, bytes]:
        dek = os.urandom(32)
        nonce = os.urandom(12)
        return dek, nonce + self._aead.encrypt(nonce, dek, self._aad(context))

    def decrypt_data_key(self, wrapped: bytes, context: dict[str, str]) -> bytes:
        return self._aead.decrypt(wrapped[:12], wrapped[12:], self._aad(context))


class AwsKms:
    def __init__(self, key_id: str, region: str) -> None:
        import boto3

        self._client = boto3.client("kms", region_name=region)
        self._key_id = key_id

    def generate_data_key(self, context: dict[str, str]) -> tuple[bytes, bytes]:
        resp = self._client.generate_data_key(KeyId=self._key_id, KeySpec="AES_256", EncryptionContext=context)
        return resp["Plaintext"], resp["CiphertextBlob"]

    def decrypt_data_key(self, wrapped: bytes, context: dict[str, str]) -> bytes:
        resp = self._client.decrypt(CiphertextBlob=wrapped, EncryptionContext=context, KeyId=self._key_id)
        plaintext: bytes = resp["Plaintext"]
        return plaintext


def get_kms() -> Kms:
    settings = get_settings()
    if settings.kms == "aws":
        if not settings.kms_key_id:
            raise AppError("NOT_CONFIGURED", "RE_KMS_KEY_ID is required when RE_KMS=aws")
        return AwsKms(settings.kms_key_id, settings.s3_region)
    return LocalKms(settings.local_master_key.get_secret_value())


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    wrapped_dek: bytes


def seal(plaintext: str, context: dict[str, str], kms: Kms | None = None) -> Sealed:
    kms = kms or get_kms()
    dek, wrapped = kms.generate_data_key(context)
    nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode(), None)
    return Sealed(nonce + ct, wrapped)


def unseal(sealed: Sealed, context: dict[str, str], kms: Kms | None = None) -> str:
    kms = kms or get_kms()
    dek = kms.decrypt_data_key(sealed.wrapped_dek, context)
    return AESGCM(dek).decrypt(sealed.ciphertext[:12], sealed.ciphertext[12:], None).decode()


# --------------------------------------------------------------- tokens ----
def sign_token(claims: dict[str, Any], audience: str, ttl_seconds: int, key: str | None = None) -> str:
    now = int(time.time())
    payload = {**claims, "aud": audience, "iat": now, "exp": now + ttl_seconds, "iss": "reliability"}
    return jwt.encode(payload, key or get_settings().token_signing_key.get_secret_value(), algorithm="HS256")


def verify_token(token: str, audience: str, key: str | None = None) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key or get_settings().token_signing_key.get_secret_value(),
            algorithms=["HS256"],
            audience=audience,
            issuer="reliability",
        )
    except jwt.PyJWTError as exc:
        raise AppError("UNAUTHENTICATED", "invalid or expired token") from exc
    return claims


# ------------------------------------------------- sealing to a runner ----
SEAL_INFO = b"reliability-lease-secrets-v1"


def seal_for_x25519(recipient_public_b64: str, plaintext: bytes) -> str:
    """ECIES-style sealing (X25519 + HKDF-SHA256 + AES-256-GCM) so lease secrets can only be
    opened by the runner process holding the matching private key."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    try:
        peer = X25519PublicKey.from_public_bytes(base64.b64decode(recipient_public_b64))
    except ValueError as exc:
        raise AppError("VALIDATION_FAILED", "invalid runner public key") from exc
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = HKDF(hashes.SHA256(), 32, salt=eph_pub, info=SEAL_INFO).derive(eph.exchange(peer))
    nonce = os.urandom(12)
    return base64.b64encode(eph_pub + nonce + AESGCM(key).encrypt(nonce, plaintext, None)).decode()
