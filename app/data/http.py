"""Shared async HTTP client for data sources."""
from __future__ import annotations

import httpx

import config


def make_client(timeout: float | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout or config.HTTP_TIMEOUT, connect=10.0),
        headers={
            "User-Agent": config.HTTP_USER_AGENT,
            "Accept": "application/json, application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        follow_redirects=True,
    )
