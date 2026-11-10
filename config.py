"""
Agentic World Simulator - central configuration.

Every value can be overridden by an environment variable of the same name
(docker-compose.yml passes several of them). This is a local-only app: there
are no sign-ups; the accounts in ADMIN_USERS are the only logins.
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv(path: Path = Path(__file__).resolve().parent / ".env") -> None:
    """Read KEY=VALUE lines from .env next to this file (real environment variables win)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Admin logins (username -> password). Local app, no sign-up flow.
# Change these before letting anyone else use this machine.
# ---------------------------------------------------------------------------
ADMIN_USERS: dict[str, str] = {
    "admin": "admin123",
}

APP_NAME = "Agentic World Simulator"
APP_VERSION = "1.0.0"
APP_HOST = _env("APP_HOST", "127.0.0.1")
APP_PORT = _env_int("APP_PORT", 8000)
SECRET_KEY = _env("APP_SECRET_KEY", "local-dev-secret-change-me-5b1f7c2e")
SESSION_MAX_AGE = 60 * 60 * 12  # seconds

# ---------------------------------------------------------------------------
# Database (PostgreSQL + pgvector, async via asyncpg)
# ---------------------------------------------------------------------------
DATABASE_URL = _env(
    "DATABASE_URL", "postgresql+asyncpg://worldsim:worldsim@localhost:5434/worldsim"
)
DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 10)

# ---------------------------------------------------------------------------
# AI failover hierarchy - tried strictly in this order for every request:
#   1. OpenAI Gemma  - Gemma behind the LAN gateway (OPENAI_GEMMA_URL), called with the `openai` package
#   2. Local Gemma   - Gemma on this PC (Ollama container)
#   3. Anthropic     - Claude API
#   4. unavailable   - "AI is not present": the app does not run
# ---------------------------------------------------------------------------
_OPENAI_GEMMA_URL = _env("OPENAI_GEMMA_URL", "http://192.168.0.101:8000/v1")

AI_PROVIDERS: list[dict] = [
    {
        "name": "openai-gemma",
        "label": "OpenAI Gemma (LAN gateway)",
        "kind": "openai",  # OpenAI-compatible API through the official `openai` package
        # The "Local AI gateway" on the LAN: OpenAI-compatible /v1 that load-balances the llama.cpp
        # Gemma 3 servers. It prints its own base_url / api_key / model when it starts. Several
        # comma-separated URLs are allowed (its DHCP address changes): the first that answers is used.
        "base_url": _OPENAI_GEMMA_URL,
        "api_key": _env("OPENAI_GEMMA_API_KEY", ""),
        "model": _env("OPENAI_GEMMA_MODEL", "gemma"),  # the gateway's model name; "auto" = ask the server
        "timeout": _env_float("OPENAI_GEMMA_TIMEOUT", 300),
        "enabled": _env_bool("OPENAI_GEMMA_ENABLED", True),
    },
    {
        "name": "local-gemma",
        "label": "Local Gemma (this PC)",
        "kind": "openai",
        # Ollama on this PC (docker compose service "ollama"; inside compose: http://ollama:11434/v1).
        # Docker Model Runner alternative: http://localhost:12434/engines/v1 with model "ai/gemma3".
        "base_url": _env("LOCAL_GEMMA_URL", "http://localhost:11434/v1"),
        "api_key": _env("LOCAL_GEMMA_API_KEY", "ollama"),
        "model": _env("LOCAL_GEMMA_MODEL", "gemma3:4b"),
        "timeout": _env_float("LOCAL_GEMMA_TIMEOUT", 900),  # CPU-only inference is slow
        "enabled": _env_bool("LOCAL_GEMMA_ENABLED", True),
    },
    {
        "name": "anthropic",
        "label": "Anthropic (Claude)",
        "kind": "anthropic",
        "api_key": _env("ANTHROPIC_API_KEY", ""),
        "model": _env("ANTHROPIC_MODEL", "claude-opus-5-5"),
        "timeout": _env_float("ANTHROPIC_TIMEOUT", 600),
        "enabled": _env_bool("ANTHROPIC_ENABLED", True),
    },
]

# "exit": refuse to start when no AI provider answers (prints "AI is not present").
# "lock": start, but keep the whole UI locked behind an "AI is not present" screen.
AI_STARTUP_POLICY = _env("AI_STARTUP_POLICY", "exit")
AI_PROBE_TIMEOUT = _env_float("AI_PROBE_TIMEOUT", 120)
AI_HEALTHCHECK_SECONDS = _env_int("AI_HEALTHCHECK_SECONDS", 60)
AI_STATUS_REFRESH_SECONDS = _env_int("AI_STATUS_REFRESH_SECONDS", 300)

CLAUDE_EFFORT = {"fast": "low", "deep": "high"}
CLAUDE_MAX_TOKENS = 16000
GEMMA_MAX_TOKENS = {"fast": 2048, "deep": 3072}
GEMMA_TEMPERATURE = 0.3

# ---------------------------------------------------------------------------
# Spam protection / overload guard for the AI
# ---------------------------------------------------------------------------
AI_MAX_CONCURRENCY = _env_int("AI_MAX_CONCURRENCY", 2)    # simultaneous LLM calls
AI_MAX_WAITING = _env_int("AI_MAX_WAITING", 24)           # calls allowed to queue for a slot
AI_INTERACTIVE_WAIT_SECONDS = 45                          # UI calls give up after this
AI_GLOBAL_RATE_PER_MIN = _env_int("AI_GLOBAL_RATE_PER_MIN", 40)
USER_AI_RATE_PER_MIN = _env_int("USER_AI_RATE_PER_MIN", 6)  # per admin, user-triggered AI actions
USER_AI_BURST = 3
API_RATE_PER_MIN = 600                                    # every /api call, per client IP
AI_MAX_INPUT_CHARS = 24000                                # hard cap on prompt size
CHAT_MAX_MESSAGE_CHARS = 2000
AI_CACHE_TTL_SECONDS = 900                                # identical prompt -> cached answer
AI_CACHE_MAX_ENTRIES = 256
ANALYSIS_COOLDOWN_SECONDS = 900                           # same analysis (e.g. market:IN) at most every 15 min
CLAUDE_DAILY_CALL_LIMIT = _env_int("CLAUDE_DAILY_CALL_LIMIT", 150)
CLAUDE_DAILY_TOKEN_LIMIT = _env_int("CLAUDE_DAILY_TOKEN_LIMIT", 1_500_000)
CIRCUIT_BREAKER_FAILURES = 3
CIRCUIT_BREAKER_COOLDOWN = 120
AI_AUTH_FAILURE_COOLDOWN = 1800   # wrong API key: leave that tier alone for 30 min
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 900

# ---------------------------------------------------------------------------
# Embeddings for pgvector (EmbeddingGemma via Ollama, local hashing fallback)
# ---------------------------------------------------------------------------
EMBEDDING_DIM = 768
EMBEDDING_BASE_URL = _env("EMBEDDING_URL", _env("LOCAL_GEMMA_URL", "http://localhost:11434/v1"))
EMBEDDING_MODEL = _env("EMBEDDING_MODEL", "embeddinggemma")
EMBEDDING_API_KEY = _env("EMBEDDING_API_KEY", "ollama")

# ---------------------------------------------------------------------------
# Data refresh cadence (seconds)
# ---------------------------------------------------------------------------
NEWS_REFRESH_SECONDS = 24 * 3600        # global headlines: once every 24 hours
MARKETS_REFRESH_SECONDS = _env_int("MARKETS_REFRESH_SECONDS", 3600)
HAZARDS_REFRESH_SECONDS = _env_int("HAZARDS_REFRESH_SECONDS", 900)
RISK_SCAN_SECONDS = 6 * 3600
PERSONA_PULSE_SECONDS = 24 * 3600
WORLDBANK_REFRESH_SECONDS = 7 * 24 * 3600
CALIBRATION_REFRESH_SECONDS = 7 * 24 * 3600
REEMBED_SECONDS = 6 * 3600

NEWS_MAX_AGE_DAYS = 3
NEWS_MAX_PER_SOURCE = 40
NEWS_ENRICH_MAX = 240
NEWS_ENRICH_BATCH = 20
HTTP_TIMEOUT = 25
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

# Market alerts ("the market is down") - percentages
MARKET_DOWN_1D = -1.5
MARKET_DOWN_5D = -3.5
MARKET_DRAWDOWN = -10.0

# Hazards
HAZARD_SIGNIFICANT_SEVERITY = 0.55
HQ_ALERT_RADII_KM = {7.0: 900, 6.0: 450, 5.0: 220, 4.0: 100}
SEISMIC_SCAN_RADIUS_KM = 300

# ---------------------------------------------------------------------------
# Simulation engine
# ---------------------------------------------------------------------------
SIM_DEFAULT_CONSUMERS = 200_000
SIM_MAX_CONSUMERS = 5_000_000
SIM_DEFAULT_TICKS = 180
SIM_MAX_TICKS = 730
SIM_SHARDS = _env_int("SIM_SHARDS", 0)            # 0 = auto (one per core, max 8)
SIM_USE_PROCESSES = _env_bool("SIM_USE_PROCESSES", True)
SIM_TRADERS_PER_MARKET = 20_000
SIM_MAX_REGIONS = 10

# Background task lanes -> worker count
TASK_LANES = {"io": 2, "ai": 2, "sim": 1}
