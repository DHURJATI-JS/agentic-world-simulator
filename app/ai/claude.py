"""
Claude tier (third in the failover chain) via the official Anthropic Python SDK.

- Model: claude-opus-5-5 by default (config.AI_PROVIDERS / ANTHROPIC_MODEL).
- Structured JSON through output_config.format (json_schema).
- Thinking is always on for Opus 5.5; depth is controlled with output_config.effort.
- Server-side refusal fallbacks are enabled (fallbacks="default").
"""
from __future__ import annotations

import time

import config
from app.ai.base import AIRequest, AIResult, BaseProvider, ProviderError

try:  # the SDK ships a compiled JSON parser; keep the app importable if it cannot load
    import anthropic
except Exception as _exc:  # noqa: BLE001
    anthropic = None  # type: ignore[assignment]
    _IMPORT_ERROR = f"anthropic SDK unavailable: {_exc}"
else:
    _IMPORT_ERROR = None

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider(BaseProvider):
    kind = "anthropic"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.api_key: str = cfg.get("api_key") or ""
        self.status.model = cfg.get("model") or "claude-opus-5-5"
        self.client = None
        if anthropic is not None and self.api_key:
            self.client = anthropic.AsyncAnthropic(
                api_key=self.api_key, timeout=self.timeout, max_retries=1
            )

    @property
    def configured(self) -> bool:
        return self.client is not None

    def _not_configured_reason(self) -> str:
        if _IMPORT_ERROR:
            return _IMPORT_ERROR
        return "ANTHROPIC_API_KEY is not set"

    def _translate(self, exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        if anthropic is None:
            return ProviderError(self.name, str(exc))
        if isinstance(exc, anthropic.AuthenticationError):
            return ProviderError(self.name, "invalid Anthropic API key", kind="auth")
        if isinstance(exc, anthropic.PermissionDeniedError):
            return ProviderError(self.name, "API key lacks permission for this model", kind="auth")
        if isinstance(exc, anthropic.NotFoundError):
            return ProviderError(self.name, f"model {self.status.model} not found", kind="bad_request")
        if isinstance(exc, anthropic.RateLimitError):
            return ProviderError(self.name, "Anthropic rate limit reached", kind="server")
        if isinstance(exc, anthropic.BadRequestError):
            return ProviderError(self.name, f"bad request: {exc.message}"[:300], kind="bad_request")
        if isinstance(exc, anthropic.APIStatusError):
            return ProviderError(self.name, f"Anthropic API error {exc.status_code}", kind="server")
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderError(self.name, "request timed out", kind="timeout")
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderError(self.name, "cannot reach api.anthropic.com", kind="unreachable")
        return ProviderError(self.name, f"{type(exc).__name__}: {exc}"[:300])

    async def probe(self, timeout: float, deep: bool = True) -> str:
        if not self.configured:
            raise ProviderError(self.name, self._not_configured_reason(), kind="auth")
        try:
            # Free call: validates the key and that the model exists, without generating tokens.
            info = await self.client.with_options(timeout=timeout).models.retrieve(self.status.model)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from None
        return getattr(info, "id", self.status.model)

    async def generate(self, request: AIRequest) -> AIResult:
        if not self.configured:
            raise ProviderError(self.name, self._not_configured_reason(), kind="auth")
        output_config: dict = {"effort": config.CLAUDE_EFFORT.get(request.depth, "medium")}
        if request.schema:
            output_config["format"] = {"type": "json_schema", "schema": request.schema}
        started = time.perf_counter()
        try:
            resp = await self.client.beta.messages.create(
                model=self.status.model,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                system=request.system,
                messages=[{"role": "user", "content": request.prompt}],
                output_config=output_config,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from None
        latency = int((time.perf_counter() - started) * 1000)

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ProviderError(self.name, f"request declined (category: {category})", kind="refusal")
        if resp.stop_reason == "max_tokens":
            raise ProviderError(self.name, "response truncated (max tokens)", kind="truncated")

        text = "".join(block.text for block in resp.content if block.type == "text")
        if not text.strip():
            raise ProviderError(self.name, "empty response", kind="server")
        usage = resp.usage
        return AIResult(
            text=text,
            provider=self.name,
            provider_label=self.label,
            model=resp.model or self.status.model,
            latency_ms=latency,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
