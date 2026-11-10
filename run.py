"""
Start the Agentic World Simulator natively:   .venv\\Scripts\\python run.py

Before the web server starts, the AI hierarchy is checked
(OpenAI Gemma -> Local Gemma -> Anthropic). If none answers, the app does not run.
Settings come from config.py, overridable by environment variables or a .env file.
"""
from __future__ import annotations

import asyncio
import os
import sys


def ensure_sqlalchemy_loads() -> None:
    """Use SQLAlchemy's compiled extension when Windows allows it, else its pure-Python fallback."""
    try:
        import sqlalchemy.util._collections_cy  # noqa: F401
    except ImportError:
        os.environ["DISABLE_SQLALCHEMY_CEXT_RUNTIME"] = "1"
        for name in [m for m in sys.modules if m == "sqlalchemy" or m.startswith("sqlalchemy.")]:
            del sys.modules[name]


def main() -> int:
    ensure_sqlalchemy_loads()

    import uvicorn

    import config
    from app.ai.router import AIUnavailable, build_router, render_unavailable_banner

    if config.AI_STARTUP_POLICY == "exit":
        router = build_router()
        try:
            asyncio.run(router.startup_check())
        except AIUnavailable as exc:
            print(render_unavailable_banner(exc.statuses), file=sys.stderr, flush=True)
            return 1

    uvicorn.run(
        "app.main:app",
        host=config.APP_HOST,
        port=config.APP_PORT,
        log_level="info",
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
