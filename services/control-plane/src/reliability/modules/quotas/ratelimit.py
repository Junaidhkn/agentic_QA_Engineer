"""Token-bucket rate limits in Redis (api-contract.md §3).

On Redis failure, ``read`` and ``write`` classes fail open; ``run``, ``expensive`` and
``verify`` fail closed (they start work that costs money or sends traffic).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from reliability.platform.errors import AppError
from reliability.platform.redis_client import get_redis

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limit:
    capacity: int
    per_seconds: int
    fail_closed: bool


CLASSES: dict[str, Limit] = {
    "read": Limit(600, 60, False),
    "write": Limit(120, 60, False),
    "expensive": Limit(20, 60, True),
    "run": Limit(30, 60, True),
    "verify": Limit(10, 60, True),
    "auth": Limit(20, 60, False),
    "runner": Limit(1200, 60, True),
}

_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + (now - ts) * rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(capacity / rate) + 1)
return {allowed, tostring(tokens)}
"""


async def check(principal: str, cls: str) -> None:
    limit = CLASSES[cls]
    rate = limit.capacity / limit.per_seconds
    try:
        allowed, tokens = await get_redis().eval(  # type: ignore[misc]
            _LUA, 1, f"rl:{cls}:{principal}", str(limit.capacity), str(rate), str(time.time())
        )
    except Exception as exc:
        log.warning("rate limiter unavailable", extra={"class": cls})
        if limit.fail_closed:
            raise AppError(
                "UNAVAILABLE", "rate limiter unavailable; try again shortly", headers={"Retry-After": "5"}
            ) from exc
        return
    if int(allowed) != 1:
        retry_after = max(1, math.ceil((1 - float(tokens)) / rate))
        raise AppError(
            "RATE_LIMITED",
            f"rate limit exceeded for {cls} requests",
            {"class": cls},
            headers={"Retry-After": str(retry_after)},
        )
