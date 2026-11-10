"""
Gemma tiers through the OpenAI-compatible API using the official `openai` package
(LAN "Local AI gateway", llama.cpp server, Ollama, vLLM, Docker Model Runner).

- base_url may list several comma-separated endpoints (e.g. a gateway whose DHCP address
  changes): the first one that answers is used, and the others are tried when it stops answering.
- If a backend's context window is too small for a prompt, the request is retried once on the
  same tier with a condensed prompt before the router falls through to the next tier.
"""
from __future__ import annotations

import time

import httpx
import openai

import config
from app.ai.base import AIRequest, AIResult, BaseProvider, ProviderError

CONNECT_TIMEOUT = 8.0  # a dead LAN host should fail fast so the next endpoint / tier takes over
_CONTEXT_WORDS = ("context", "too long", "exceed", "n_ctx", "tokens")


def _timeout(seconds: float) -> httpx.Timeout:
    return httpx.Timeout(seconds, connect=min(CONNECT_TIMEOUT, seconds))


def squeeze(prompt: str, keep_ratio: float = 0.5) -> str:
    """Shorten a prompt from the middle: the start (profile, data) and the end (task) survive."""
    if len(prompt) < 3000:
        return prompt
    keep = int(len(prompt) * keep_ratio)
    head = int(keep * 0.6)
    tail = keep - head
    return prompt[:head] + "\n[... part of the context was removed to fit the model's context window ...]\n" + prompt[-tail:]


class OpenAICompatProvider(BaseProvider):
    kind = "openai"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.base_urls: list[str] = [u.strip().rstrip("/") for u in str(cfg.get("base_url") or "").split(",") if u.strip()]
        self.api_key: str = cfg.get("api_key") or "none"
        self._wanted_model: str = cfg.get("model") or "auto"
        # JSON strategy degrades if a server rejects json_schema: schema -> object -> none
        self._json_mode = "schema"
        self._clients = {
            url: openai.AsyncOpenAI(base_url=url, api_key=self.api_key, timeout=_timeout(self.timeout), max_retries=0)
            for url in self.base_urls
        }
        self._active = 0

    # -- endpoints ---------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.base_urls)

    @property
    def base_url(self) -> str:
        return self.base_urls[self._active] if self.base_urls else ""

    @property
    def client(self) -> openai.AsyncOpenAI:
        return self._clients[self.base_url]

    def _order(self) -> list[int]:
        """Active endpoint first, then the others in configured order."""
        return [self._active] + [i for i in range(len(self.base_urls)) if i != self._active]

    async def _resolve_model(self, client: openai.AsyncOpenAI, timeout: float) -> str:
        client = client.with_options(timeout=_timeout(timeout))
        if self._wanted_model and self._wanted_model != "auto":
            # still list models: it validates the API key and that the server is up
            try:
                await client.models.list()
            except openai.NotFoundError:
                pass  # some servers do not implement /models
            return self._wanted_model
        page = await client.models.list()
        ids = [m.id for m in page.data]
        if not ids:
            raise ProviderError(self.name, "server lists no models", kind="server")
        chat_ids = [i for i in ids if "embed" not in i.lower()]
        gemma = [i for i in chat_ids if "gemma" in i.lower()]
        return (gemma or chat_ids or ids)[0]

    def _translate(self, exc: Exception, url: str | None = None) -> ProviderError:
        url = url or self.base_url
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, openai.AuthenticationError):
            return ProviderError(self.name, f"authentication failed at {url} (check the API key)", kind="auth")
        if isinstance(exc, openai.PermissionDeniedError):
            return ProviderError(self.name, "permission denied by server", kind="auth")
        if isinstance(exc, openai.NotFoundError):
            return ProviderError(self.name, f"model or endpoint not found ({self.model or self._wanted_model})", kind="bad_request")
        if isinstance(exc, openai.APITimeoutError):
            return ProviderError(self.name, f"request timed out at {url}", kind="timeout")
        if isinstance(exc, openai.APIConnectionError):
            return ProviderError(self.name, f"unreachable at {url}", kind="unreachable")
        if isinstance(exc, openai.RateLimitError):
            return ProviderError(self.name, "server is rate limiting", kind="server")
        if isinstance(exc, openai.BadRequestError):
            return ProviderError(self.name, f"bad request: {getattr(exc, 'message', exc)}"[:300], kind="bad_request")
        if isinstance(exc, openai.APIStatusError):
            return ProviderError(self.name, f"server error {exc.status_code}", kind="server")
        return ProviderError(self.name, f"{type(exc).__name__}: {exc}"[:300])

    async def probe(self, timeout: float, deep: bool = True) -> str:
        """Find the first endpoint that answers (and authenticates); make it the active one."""
        errors: list[ProviderError] = []
        for idx in self._order():
            url = self.base_urls[idx]
            client = self._clients[url]
            try:
                model = await self._resolve_model(client, timeout)
                if deep:
                    await client.with_options(timeout=_timeout(timeout)).chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": "Reply with the single word OK."}],
                        max_tokens=4,
                        temperature=0,
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(self._translate(exc, url))
                continue
            self._active = idx
            self.status.model = model
            return model
        if not errors:
            raise ProviderError(self.name, "no endpoint configured", kind="unreachable")
        # report the most meaningful failure: a wrong key beats "unreachable"
        auth = next((e for e in errors if e.kind == "auth"), None)
        if auth is not None:
            raise auth
        if len(errors) == 1:
            raise errors[0]
        raise ProviderError(self.name, "; ".join(str(e) for e in errors)[:400], kind=errors[0].kind)

    def _response_format(self, request: AIRequest) -> dict | None:
        if not request.schema or self._json_mode == "none":
            return None
        if self._json_mode == "schema":
            return {
                "type": "json_schema",
                "json_schema": {"name": request.schema_name, "schema": request.schema, "strict": True},
            }
        return {"type": "json_object"}

    async def _complete(self, request: AIRequest, prompt: str, max_tokens: int) -> object:
        """One chat completion on the active endpoint, degrading the JSON mode if the server rejects it."""
        temperature = config.GEMMA_TEMPERATURE if request.temperature is None else request.temperature
        system = request.system
        if request.schema:
            system += "\nRespond with a single JSON object only - no prose, no code fences."
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        for _attempt in range(3):
            kwargs: dict = {"model": self.status.model, "messages": messages, "max_tokens": max_tokens,
                            "temperature": temperature}
            fmt = self._response_format(request)
            if fmt:
                kwargs["response_format"] = fmt
            try:
                return await self.client.chat.completions.create(**kwargs)
            except openai.BadRequestError as exc:
                message = str(getattr(exc, "message", exc)).lower()
                if any(word in message for word in _CONTEXT_WORDS):
                    raise ProviderError(self.name, "prompt too long for this model's context", kind="context") from None
                # Older servers reject json_schema / json_object: degrade and retry.
                if fmt and self._json_mode == "schema":
                    self._json_mode = "object"
                    continue
                if fmt and self._json_mode == "object":
                    self._json_mode = "none"
                    continue
                raise self._translate(exc) from None
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from None
        raise ProviderError(self.name, "could not complete request")

    async def generate(self, request: AIRequest) -> AIResult:
        if not self.status.model:
            await self.probe(self.timeout, deep=False)
        max_tokens = request.max_tokens or config.GEMMA_MAX_TOKENS.get(request.depth, 2048)
        started = time.perf_counter()
        try:
            resp = await self._complete(request, request.prompt, max_tokens)
        except ProviderError as exc:
            if exc.kind == "unreachable" and len(self.base_urls) > 1:
                # the active endpoint went away (e.g. the gateway changed address): find another one
                await self.probe(min(30.0, self.timeout), deep=False)
                resp = await self._complete(request, request.prompt, max_tokens)
            elif exc.kind == "context":
                # the backend's per-slot context window is small: retry once with a condensed prompt
                resp = await self._complete(request, squeeze(request.prompt), min(max_tokens, 1536))
            else:
                raise
        latency = int((time.perf_counter() - started) * 1000)
        choice = resp.choices[0] if resp.choices else None
        text = (choice.message.content or "") if choice else ""
        if not text.strip():
            raise ProviderError(self.name, "empty response", kind="server")
        if choice is not None and choice.finish_reason == "length" and request.schema:
            raise ProviderError(self.name, "response truncated (max tokens)", kind="truncated")
        usage = resp.usage
        return AIResult(
            text=text,
            provider=self.name,
            provider_label=self.label,
            model=resp.model or self.status.model,
            latency_ms=latency,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
