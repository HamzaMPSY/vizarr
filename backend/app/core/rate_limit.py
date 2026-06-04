from __future__ import annotations

import time
from dataclasses import dataclass

import redis.asyncio as redis
from redis.exceptions import RedisError


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


class ApiKeyRateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int,
        redis_client: redis.Redis | None = None,
    ) -> None:
        self._limit = max(int(limit), 0)
        self._window_seconds = max(int(window_seconds), 1)
        self._redis = redis_client
        self._memory: dict[str, tuple[int, int, float]] = {}

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    async def check(self, token_digest: str) -> RateLimitResult:
        if not self.enabled:
            return RateLimitResult(allowed=True, limit=0, remaining=0, retry_after=0)
        if self._redis is not None:
            try:
                return await self._check_redis(token_digest)
            except RedisError:
                return self._check_memory(token_digest)
        return self._check_memory(token_digest)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
        self._memory.clear()

    async def _check_redis(self, token_digest: str) -> RateLimitResult:
        key = rate_limit_key(token_digest)
        count = int(await self._redis.incr(key))
        if count == 1:
            await self._redis.expire(key, self._window_seconds)
        ttl = await self._redis.ttl(key)
        retry_after = self._window_seconds if ttl is None or int(ttl) < 0 else int(ttl)
        remaining = max(self._limit - count, 0)
        return RateLimitResult(
            allowed=count <= self._limit,
            limit=self._limit,
            remaining=remaining,
            retry_after=retry_after,
        )

    def _check_memory(self, token_digest: str) -> RateLimitResult:
        now = time.monotonic()
        window_start, count, expires_at = self._memory.get(token_digest, (0, 0, 0.0))
        if expires_at <= now:
            window_start = int(now)
            count = 0
            expires_at = now + self._window_seconds
        count += 1
        self._memory[token_digest] = (window_start, count, expires_at)
        retry_after = max(1, int(expires_at - now))
        return RateLimitResult(
            allowed=count <= self._limit,
            limit=self._limit,
            remaining=max(self._limit - count, 0),
            retry_after=retry_after,
        )


async def connect_rate_limiter(redis_url: str, *, limit: int, window_seconds: int) -> ApiKeyRateLimiter:
    if limit <= 0:
        return ApiKeyRateLimiter(limit=limit, window_seconds=window_seconds)
    try:
        client = redis.from_url(redis_url, decode_responses=False)
        await client.ping()
        return ApiKeyRateLimiter(limit=limit, window_seconds=window_seconds, redis_client=client)
    except Exception:
        return ApiKeyRateLimiter(limit=limit, window_seconds=window_seconds)


def rate_limit_key(token_digest: str) -> str:
    return f"rate-limit:api-key:{token_digest}"
