"""
Spam protection / overload guard in front of every LLM call.

Layers (outermost first):
  1. single-flight + TTL cache  - identical prompts share one call and reuse answers
  2. global token bucket        - caps LLM calls per minute across the whole app
  3. bounded waiting room       - at most AI_MAX_WAITING callers may queue; extra are rejected
  4. concurrency semaphore      - at most AI_MAX_CONCURRENCY calls hit the model at once
  5. per-provider circuit breakers and a daily Claude budget (used by the router)
Per-user limits for HTTP endpoints live in app.security.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone

import config
from app.ai.base import AIRequest, AIResult
from app.security import TokenBucket


class AIOverloaded(Exception):
    """Rejected by spam protection (rate limit, full queue or busy)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CircuitBreaker:
    def __init__(self, threshold: int, cooldown: float) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self.opened_at: float | None = None
        self.open_for = cooldown
        self.reason: str | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        return time.monotonic() - self.opened_at >= self.open_for  # half-open trial

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.reason = None

    def failure(self, kind: str | None = None) -> None:
        self.failures += 1
        if kind == "auth":
            # A wrong/missing API key won't fix itself: stop calling that server for a long while
            # (also avoids tripping the upstream gateway's own brute-force protection).
            self.opened_at = time.monotonic()
            self.open_for = config.AI_AUTH_FAILURE_COOLDOWN
            self.reason = "auth"
        elif self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            self.open_for = self.cooldown
            self.reason = kind

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.allow() else "open"


class AIGuard:
    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(config.AI_MAX_CONCURRENCY)
        # Background work may use all slots but one, so a person in the UI never waits behind it.
        self._bg_sem = asyncio.Semaphore(max(1, config.AI_MAX_CONCURRENCY - 1))
        self.waiting = 0
        self.active = 0
        rate = config.AI_GLOBAL_RATE_PER_MIN
        self._bucket = TokenBucket(rate / 60.0, max(4.0, rate / 6.0))
        self._cache: OrderedDict[str, tuple[float, AIResult]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self.stats: Counter = Counter()
        self._claude_day = self._today()
        self.claude_calls = 0
        self.claude_tokens = 0

    # -- cache / single flight -------------------------------------------------
    @staticmethod
    def cache_key(request: AIRequest) -> str:
        digest = hashlib.sha256()
        for part in (request.system, request.prompt, request.schema_name, request.depth, str(request.max_tokens)):
            digest.update((part or "").encode("utf-8", "ignore"))
            digest.update(b"\x00")
        return digest.hexdigest()

    def cache_get(self, key: str) -> AIResult | None:
        item = self._cache.get(key)
        if not item:
            return None
        expires, result = item
        if expires < time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return replace(result, cached=True)

    def cache_put(self, key: str, result: AIResult) -> None:
        self._cache[key] = (time.monotonic() + config.AI_CACHE_TTL_SECONDS, result)
        self._cache.move_to_end(key)
        while len(self._cache) > config.AI_CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    def cache_evict(self, key: str | None) -> None:
        if key:
            self._cache.pop(key, None)

    async def single_flight(self, key: str | None, factory: Callable[[], Awaitable[AIResult]]) -> AIResult:
        if key is None:
            return await factory()
        hit = self.cache_get(key)
        if hit is not None:
            self.stats["cache_hits"] += 1
            return hit
        pending = self._inflight.get(key)
        if pending is not None:
            self.stats["coalesced"] += 1
            return await asyncio.shield(pending)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await factory()
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as exc:
            future.set_exception(exc)
            future.exception()  # mark retrieved: waiters re-raise it, nobody else needs to
            raise
        else:
            self.cache_put(key, result)
            future.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)

    # -- admission control -----------------------------------------------------
    @asynccontextmanager
    async def slot(self, *, interactive: bool):
        while True:
            wait = self._bucket.take()
            if wait <= 0:
                break
            if interactive or wait > 90:
                self.stats["rejected_rate"] += 1
                raise AIOverloaded(
                    f"AI rate limit reached ({config.AI_GLOBAL_RATE_PER_MIN}/min). Try again shortly.",
                    retry_after=wait,
                )
            self.stats["throttled"] += 1
            await asyncio.sleep(min(wait, 5.0))

        if self.waiting >= config.AI_MAX_WAITING:
            self.stats["rejected_queue"] += 1
            raise AIOverloaded("AI queue is full - too many requests waiting.", retry_after=30)
        self.waiting += 1
        holds_bg = False
        try:
            if interactive:
                await asyncio.wait_for(self._sem.acquire(), timeout=config.AI_INTERACTIVE_WAIT_SECONDS)
            else:
                await self._bg_sem.acquire()
                holds_bg = True
                await self._sem.acquire()
        except TimeoutError:
            self.stats["rejected_busy"] += 1
            raise AIOverloaded("AI is busy with other work. Try again in a minute.", retry_after=60) from None
        except BaseException:
            if holds_bg:
                self._bg_sem.release()
            raise
        finally:
            self.waiting -= 1

        self.active += 1
        self.stats["admitted"] += 1
        try:
            yield
        finally:
            self.active -= 1
            self._sem.release()
            if holds_bg:
                self._bg_sem.release()

    # -- Claude daily budget ---------------------------------------------------
    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._claude_day:
            self._claude_day = today
            self.claude_calls = 0
            self.claude_tokens = 0

    def claude_allowed(self) -> tuple[bool, str]:
        self._roll_day()
        if self.claude_calls >= config.CLAUDE_DAILY_CALL_LIMIT:
            return False, f"daily Claude call limit ({config.CLAUDE_DAILY_CALL_LIMIT}) reached"
        if self.claude_tokens >= config.CLAUDE_DAILY_TOKEN_LIMIT:
            return False, "daily Claude token budget reached"
        return True, ""

    def claude_record(self, tokens: int) -> None:
        self._roll_day()
        self.claude_calls += 1
        self.claude_tokens += max(0, int(tokens))

    def seed_claude_usage(self, calls: int, tokens: int) -> None:
        self._roll_day()
        self.claude_calls = calls
        self.claude_tokens = tokens

    def snapshot(self) -> dict:
        return {
            "max_concurrency": config.AI_MAX_CONCURRENCY,
            "active": self.active,
            "waiting": self.waiting,
            "max_waiting": config.AI_MAX_WAITING,
            "rate_per_min": config.AI_GLOBAL_RATE_PER_MIN,
            "user_rate_per_min": config.USER_AI_RATE_PER_MIN,
            "cache_entries": len(self._cache),
            "claude_calls_today": self.claude_calls,
            "claude_call_limit": config.CLAUDE_DAILY_CALL_LIMIT,
            "claude_tokens_today": self.claude_tokens,
            "claude_token_limit": config.CLAUDE_DAILY_TOKEN_LIMIT,
            "stats": dict(self.stats),
        }
