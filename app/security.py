"""
Authentication (admin accounts from config.ADMIN_USERS) and spam protection:
token-bucket rate limits for API calls and user-triggered AI actions, plus a
login brute-force lockout.
"""
from __future__ import annotations

import hmac
import time
from collections import OrderedDict, deque

from fastapi import HTTPException, Request

import config


# ---------------------------------------------------------------------------
# Token buckets
# ---------------------------------------------------------------------------
class TokenBucket:
    __slots__ = ("rate", "capacity", "tokens", "updated")

    def __init__(self, rate_per_sec: float, capacity: float) -> None:
        self.rate = max(rate_per_sec, 1e-9)
        self.capacity = max(capacity, 1.0)
        self.tokens = self.capacity
        self.updated = time.monotonic()

    def take(self, amount: float = 1.0) -> float:
        """Consume tokens. Returns 0 on success, otherwise seconds until enough tokens exist."""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= amount:
            self.tokens -= amount
            return 0.0
        return (amount - self.tokens) / self.rate


class KeyedRateLimiter:
    """One token bucket per key (user or IP), LRU-bounded."""

    def __init__(self, per_minute: float, burst: float, max_keys: int = 5000) -> None:
        self.per_minute = per_minute
        self.burst = burst
        self.max_keys = max_keys
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self.rejected = 0

    def check(self, key: str, cost: float = 1.0) -> float:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self.per_minute / 60.0, self.burst)
            self._buckets[key] = bucket
            if len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
        wait = bucket.take(cost)
        if wait > 0:
            self.rejected += 1
        return wait


api_limiter = KeyedRateLimiter(config.API_RATE_PER_MIN, config.API_RATE_PER_MIN / 4)
user_ai_limiter = KeyedRateLimiter(config.USER_AI_RATE_PER_MIN, config.USER_AI_BURST)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------
class LoginThrottle:
    def __init__(self) -> None:
        self._failures: dict[str, deque] = {}

    def locked_for(self, ip: str) -> float:
        now = time.time()
        failures = self._failures.get(ip)
        if not failures:
            return 0.0
        while failures and now - failures[0] > config.LOGIN_LOCKOUT_SECONDS:
            failures.popleft()
        if len(failures) >= config.LOGIN_MAX_FAILURES:
            return config.LOGIN_LOCKOUT_SECONDS - (now - failures[0])
        return 0.0

    def record_failure(self, ip: str) -> None:
        self._failures.setdefault(ip, deque()).append(time.time())

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)


login_throttle = LoginThrottle()


def verify_credentials(username: str, password: str) -> bool:
    expected = config.ADMIN_USERS.get(username or "")
    if expected is None:
        hmac.compare_digest(password or "", "x" * 16)  # keep timing similar for unknown users
        return False
    return hmac.compare_digest((password or "").encode(), expected.encode())


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
def require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user or user not in config.ADMIN_USERS:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


def ai_rate_limit(request: Request) -> str:
    """Per-admin limit for endpoints that trigger AI work (spam protection)."""
    user = require_user(request)
    wait = user_ai_limiter.check(f"user:{user}")
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Slow down - AI request limit reached. Try again in {int(wait) + 1}s.",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    return user
