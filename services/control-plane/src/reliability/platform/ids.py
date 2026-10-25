"""UUIDv7 identifiers (time-ordered, non-enumerable) and opaque tokens."""

from __future__ import annotations

import os
import secrets
import time
import uuid


def uuid7() -> uuid.UUID:
    """RFC 9562 UUID version 7: 48-bit Unix ms timestamp + 74 random bits."""
    ts_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ts_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version
    value |= ((rand >> 62) & 0xFFF) << 64  # rand_a (12 bits)
    value |= 0b10 << 62  # variant
    value |= rand & ((1 << 62) - 1)  # rand_b
    return uuid.UUID(int=value)


def random_token(nbytes: int = 32) -> str:
    """URL-safe random token with ``nbytes`` of entropy (default 256 bits)."""
    return secrets.token_urlsafe(nbytes)
