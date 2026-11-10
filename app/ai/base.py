"""Shared AI types and helpers."""
from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AIRequest:
    system: str
    prompt: str
    purpose: str = "general"
    schema: dict | None = None
    schema_name: str = "result"
    depth: str = "fast"  # "fast" (classification/extraction) | "deep" (analysis)
    max_tokens: int | None = None
    temperature: float | None = None


@dataclass
class AIResult:
    text: str
    provider: str
    provider_label: str
    model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    data: Any = None
    cached: bool = False


class ProviderError(Exception):
    """A single provider failed; the router moves on to the next tier."""

    def __init__(self, provider: str, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.provider = provider
        self.kind = kind  # auth | unreachable | timeout | server | bad_request | refusal | truncated | error


@dataclass
class ProviderState:
    state: str = "unknown"  # unknown | ok | down | not_configured | disabled
    model: str = ""
    last_error: str | None = None
    last_ok_at: float | None = None
    last_checked_at: float | None = None
    last_latency_ms: int | None = None
    calls: int = 0
    failures: int = 0
    extra: dict = field(default_factory=dict)


class BaseProvider(ABC):
    kind = "base"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.name: str = cfg["name"]
        self.label: str = cfg.get("label", cfg["name"])
        self.enabled: bool = bool(cfg.get("enabled", True))
        self.timeout: float = float(cfg.get("timeout", 120))
        self.status = ProviderState(model=str(cfg.get("model", "")))

    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @property
    def model(self) -> str:
        return self.status.model

    @abstractmethod
    async def probe(self, timeout: float, deep: bool = True) -> str:
        """Verify the provider can serve requests; returns the model id. Raises ProviderError.

        deep=True also runs a tiny generation (startup). deep=False only checks the server is up and
        authenticated, so a busy-but-alive model is never reported as missing.
        """

    @abstractmethod
    async def generate(self, request: AIRequest) -> AIResult: ...

    def mark_ok(self, latency_ms: int | None = None) -> None:
        self.status.state = "ok"
        self.status.last_error = None
        self.status.last_ok_at = time.time()
        self.status.last_checked_at = time.time()
        if latency_ms is not None:
            self.status.last_latency_ms = latency_ms

    def mark_down(self, error: str) -> None:
        self.status.state = "down" if self.configured else "not_configured"
        self.status.last_error = error[:400]
        self.status.last_checked_at = time.time()

    def public_status(self) -> dict:
        state = self.status.state
        if not self.enabled:
            state = "disabled"
        elif not self.configured:
            state = "not_configured"
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "model": self.status.model,
            "state": state,
            "configured": self.configured,
            "enabled": self.enabled,
            "last_error": self.status.last_error,
            "last_ok_at": self.status.last_ok_at,
            "last_checked_at": self.status.last_checked_at,
            "last_latency_ms": self.status.last_latency_ms,
            "calls": self.status.calls,
            "failures": self.status.failures,
        }


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating code fences and leading/trailing prose."""
    if not text:
        return None
    candidates = [text.strip()]
    candidates += [m.strip() for m in _FENCE_RE.findall(text)]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
    # Fall back to the first balanced {...} or [...] block.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth = 0
            in_str = False
            escape = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except (json.JSONDecodeError, ValueError):
                            break
            start = text.find(opener, start + 1)
    return None
