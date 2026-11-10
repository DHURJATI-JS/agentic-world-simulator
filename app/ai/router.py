"""
AI failover router.

Order for every call: local Gemma -> Docker Gemma -> Claude -> failed.
If no tier answers, the app reports "AI is not present": at startup it refuses to
run, at runtime it locks the UI and pauses background work until a tier recovers.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import config
from app.ai.base import AIRequest, AIResult, BaseProvider, ProviderError, extract_json
from app.ai.claude import AnthropicProvider
from app.ai.gemma import OpenAICompatProvider
from app.ai.guard import AIGuard, AIOverloaded, CircuitBreaker

log = logging.getLogger("worldsim.ai")

# provider failures that mean "this tier is not serving". A timeout means slow, not down.
_DOWN_KINDS = {"auth", "unreachable", "server"}


class AIUnavailable(Exception):
    """No tier of the failover chain is reachable: the AI is not present."""

    def __init__(self, message: str, statuses: list[dict] | None = None) -> None:
        super().__init__(message)
        self.statuses = statuses or []


class AIRequestFailed(AIUnavailable):
    """The chain is up, but every tier failed this particular request (slow, refused, bad output...)."""


class AIBadOutput(Exception):
    """The model answered, but not with usable JSON."""


UsageRecorder = Callable[[dict], Awaitable[None]]
StatusListener = Callable[[bool, dict], Awaitable[None]]


class AIRouter:
    def __init__(self, providers: list[BaseProvider], guard: AIGuard) -> None:
        self.providers = providers
        self.guard = guard
        self.breakers = {
            p.name: CircuitBreaker(config.CIRCUIT_BREAKER_FAILURES, config.CIRCUIT_BREAKER_COOLDOWN)
            for p in providers
        }
        self.available = False
        self.active_provider: str | None = None
        self.last_failure: str | None = None
        self.checked_at: float | None = None
        self.usage_recorder: UsageRecorder | None = None
        self.status_listener: StatusListener | None = None
        self._probe_lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ probing
    async def probe_all(self, deep: bool = True) -> list[dict]:
        """Check every tier in order (all of them, so the UI can show the whole chain).

        deep=True (startup) runs a tiny generation on each tier; runtime checks are shallow so a
        model that is merely busy generating is not mistaken for an absent AI.
        """
        async with self._probe_lock:
            first_ok: str | None = None
            for provider in self.providers:
                if not provider.enabled:
                    continue
                if not provider.configured:
                    reason = getattr(provider, "_not_configured_reason", lambda: "not configured")()
                    provider.mark_down(reason)
                    continue
                breaker = self.breakers[provider.name]
                if not deep and not breaker.allow():
                    continue  # circuit open (e.g. wrong API key): leave that server alone for now
                started = time.perf_counter()
                timeout = config.AI_PROBE_TIMEOUT if deep else min(30.0, config.AI_PROBE_TIMEOUT)
                try:
                    model = await asyncio.wait_for(provider.probe(timeout, deep=deep), timeout=timeout + 10)
                except ProviderError as exc:
                    provider.mark_down(str(exc))
                    if exc.kind == "auth":
                        breaker.failure("auth")
                    continue
                except TimeoutError:
                    provider.mark_down("health probe timed out")
                    continue
                except Exception as exc:  # noqa: BLE001
                    provider.mark_down(f"{type(exc).__name__}: {exc}")
                    continue
                provider.status.model = model or provider.status.model
                provider.mark_ok(int((time.perf_counter() - started) * 1000) if deep else None)
                if deep:
                    self.breakers[provider.name].success()
                if first_ok is None:
                    first_ok = provider.name
            self.checked_at = time.time()
            reason = None if first_ok else self._chain_failure_text()
            await self._set_available(first_ok is not None, first_ok, reason)
            return self.provider_statuses()

    async def startup_check(self) -> list[dict]:
        statuses = await self.probe_all()
        if not self.available:
            raise AIUnavailable("AI is not present", statuses)
        return statuses

    async def health_loop(self) -> None:
        while True:
            await asyncio.sleep(config.AI_HEALTHCHECK_SECONDS)
            try:
                stale = time.time() - (self.checked_at or 0) > config.AI_STATUS_REFRESH_SECONDS
                if not self.available or stale:
                    await self.probe_all(deep=False)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("AI health probe failed")

    # ------------------------------------------------------------------ status
    def provider_statuses(self) -> list[dict]:
        out = []
        for index, provider in enumerate(self.providers, start=1):
            item = provider.public_status()
            item["order"] = index
            item["circuit"] = self.breakers[provider.name].state
            item["endpoint"] = getattr(provider, "base_url", "api.anthropic.com")
            out.append(item)
        return out

    def _chain_failure_text(self) -> str:
        parts = []
        for provider in self.providers:
            if not provider.enabled:
                continue
            parts.append(f"{provider.label}: {provider.status.last_error or 'unavailable'}")
        return "; ".join(parts) or "no AI provider is enabled"

    def status(self) -> dict:
        active = next((p for p in self.providers if p.name == self.active_provider), None)
        return {
            "available": self.available,
            "active": self.active_provider if self.available else None,
            "active_label": active.label if (active and self.available) else None,
            "active_model": active.model if (active and self.available) else None,
            "is_fallback": bool(self.available and active and self.providers and active is not self.providers[0]),
            "message": None if self.available else "AI is not present",
            "detail": None if self.available else (self.last_failure or self._chain_failure_text()),
            "checked_at": self.checked_at,
            "providers": self.provider_statuses(),
            "guard": self.guard.snapshot(),
        }

    async def _set_available(self, ok: bool, provider: str | None, reason: str | None) -> None:
        changed = ok != self.available
        self.available = ok
        if provider:
            self.active_provider = provider
        self.last_failure = None if ok else reason
        if changed and self.status_listener is not None:
            try:
                await self.status_listener(ok, self.status())
            except Exception:  # noqa: BLE001
                log.exception("AI status listener failed")

    # ------------------------------------------------------------------ calls
    async def generate(
        self, request: AIRequest, *, interactive: bool = False, use_cache: bool = True
    ) -> AIResult:
        if not self.available:
            raise AIUnavailable("AI is not present", self.provider_statuses())
        budget = config.AI_MAX_INPUT_CHARS
        if len(request.system) + len(request.prompt) > budget:
            keep = max(1000, budget - len(request.system))
            request = replace(request, prompt=request.prompt[:keep] + "\n[...input truncated by guard...]")
        key = self.guard.cache_key(request) if use_cache else None
        return await self.guard.single_flight(key, lambda: self._admitted(request, interactive))

    async def _admitted(self, request: AIRequest, interactive: bool) -> AIResult:
        async with self.guard.slot(interactive=interactive):
            return await self._failover(request)

    async def _failover(self, request: AIRequest) -> AIResult:
        errors: list[str] = []
        for provider in self.providers:
            if not provider.enabled or not provider.configured:
                continue
            breaker = self.breakers[provider.name]
            if not breaker.allow():
                errors.append(f"{provider.label}: skipped (circuit open after repeated failures)")
                continue
            if provider.kind == "anthropic":
                allowed, why = self.guard.claude_allowed()
                if not allowed:
                    errors.append(f"{provider.label}: {why}")
                    continue
            provider.status.calls += 1
            started = time.perf_counter()
            try:
                result = await provider.generate(request)
            except ProviderError as exc:
                provider.status.failures += 1
                breaker.failure(exc.kind)
                if exc.kind in _DOWN_KINDS:
                    provider.mark_down(str(exc))
                else:
                    provider.status.last_error = f"last request: {exc}"[:400]
                errors.append(f"{provider.label}: {exc}")
                await self._record(provider, request, ok=False, error=str(exc),
                                   latency_ms=int((time.perf_counter() - started) * 1000))
                log.warning("AI tier %s failed (%s) - trying next tier", provider.name, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                provider.status.failures += 1
                breaker.failure()
                errors.append(f"{provider.label}: {type(exc).__name__}: {exc}")
                await self._record(provider, request, ok=False, error=str(exc),
                                   latency_ms=int((time.perf_counter() - started) * 1000))
                log.exception("AI tier %s crashed", provider.name)
                continue

            breaker.success()
            provider.mark_ok(result.latency_ms)
            if provider.kind == "anthropic":
                self.guard.claude_record(result.input_tokens + result.output_tokens)
            self.active_provider = provider.name
            await self._record(provider, request, ok=True, result=result)
            return result

        reason = "; ".join(errors) or "no AI provider is configured"
        self._spawn(self._recheck(reason))
        raise AIRequestFailed(f"Every AI tier failed this request - {reason}", self.provider_statuses())

    async def generate_json(
        self,
        request: AIRequest,
        *,
        interactive: bool = False,
        use_cache: bool = True,
        validator: Callable[[Any], bool] | None = None,
    ) -> tuple[Any, AIResult]:
        """Generate and parse JSON. One automatic repair attempt on malformed output."""
        result = await self.generate(request, interactive=interactive, use_cache=use_cache)
        data = extract_json(result.text)
        if data is None or (validator is not None and not validator(data)):
            self.guard.cache_evict(self.guard.cache_key(request))
            repair = replace(
                request,
                prompt=request.prompt
                + "\n\nIMPORTANT: your previous reply was not valid JSON for the required schema. "
                "Reply again with ONLY the JSON object.",
            )
            result = await self.generate(repair, interactive=interactive, use_cache=False)
            data = extract_json(result.text)
            if data is None or (validator is not None and not validator(data)):
                raise AIBadOutput(f"{result.provider_label} returned output that is not valid JSON")
        result.data = data
        return data, result

    # ------------------------------------------------------------------ helpers
    async def _recheck(self, reason: str) -> None:
        """After a total failure, verify whether the AI is really gone (vs. one slow or bad request)."""
        try:
            await self.probe_all(deep=False)
            if not self.available:
                self.last_failure = reason
        except Exception:  # noqa: BLE001
            log.exception("AI re-check failed")

    def _spawn(self, coro: Awaitable) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _record(
        self,
        provider: BaseProvider,
        request: AIRequest,
        *,
        ok: bool,
        result: AIResult | None = None,
        error: str | None = None,
        latency_ms: int = 0,
    ) -> None:
        if self.usage_recorder is None:
            return
        row = {
            "provider": provider.name,
            "model": (result.model if result else provider.model) or "",
            "purpose": request.purpose,
            "ok": ok,
            "error": error,
            "latency_ms": result.latency_ms if result else latency_ms,
            "input_tokens": result.input_tokens if result else 0,
            "output_tokens": result.output_tokens if result else 0,
        }
        try:
            await self.usage_recorder(row)
        except Exception:  # noqa: BLE001
            log.debug("usage recording failed", exc_info=True)


def build_router() -> AIRouter:
    providers: list[BaseProvider] = []
    for cfg in config.AI_PROVIDERS:
        if cfg["kind"] == "anthropic":
            providers.append(AnthropicProvider(cfg))
        else:
            providers.append(OpenAICompatProvider(cfg))
    return AIRouter(providers, AIGuard())


def render_unavailable_banner(statuses: list[dict]) -> str:
    line = "=" * 78
    rows = [
        "",
        line,
        "  AI IS NOT PRESENT (unavailable) - the Agentic World Simulator will not run.",
        line,
        "  Hierarchy checked in order (OpenAI Gemma -> Local Gemma -> Anthropic -> unavailable):",
    ]
    for item in statuses:
        state = item.get("state", "unknown").upper().replace("_", " ")
        where = item.get("endpoint") or ""
        rows.append(f"    {item.get('order', '?')}. {item.get('label', item.get('name'))}  [{state}]  {where}")
        if item.get("last_error"):
            rows.append(f"         -> {item['last_error']}")
    rows += [
        "",
        "  Fix at least one tier and start again:",
        "    - OpenAI Gemma: set OPENAI_GEMMA_API_KEY (and OPENAI_GEMMA_URL) in .env or config.py",
        "    - Local Gemma : docker compose up -d ollama ollama-pull",
        "    - Anthropic   : set ANTHROPIC_API_KEY",
        line,
        "",
    ]
    return "\n".join(rows)


__all__ = [
    "AIBadOutput",
    "AIOverloaded",
    "AIRequestFailed",
    "AIRouter",
    "AIUnavailable",
    "build_router",
    "render_unavailable_banner",
]
