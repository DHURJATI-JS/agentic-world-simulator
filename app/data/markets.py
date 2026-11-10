"""Market data (Yahoo Finance chart API), analytics, Monte Carlo outlook and 'market down' alerts."""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

import config
from app.data.http import make_client
from app.data.sources import MARKETS
from app.db import SessionLocal
from app.models import MarketQuote, utcnow

log = logging.getLogger("worldsim.markets")

CHART_URLS = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
)


async def fetch_chart(client, symbol: str, rng: str = "1y") -> dict:
    last_error = None
    for template in CHART_URLS:
        try:
            resp = await client.get(template.format(symbol=symbol), params={"range": rng, "interval": "1d"})
            if resp.status_code == 429:
                last_error = "rate limited"
                await asyncio.sleep(1.5)
                continue
            resp.raise_for_status()
            payload = resp.json()
            result = (payload.get("chart") or {}).get("result") or []
            if not result:
                err = (payload.get("chart") or {}).get("error") or {}
                raise ValueError(err.get("description") or "no data")
            res = result[0]
            meta = res.get("meta") or {}
            stamps = res.get("timestamp") or []
            closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            history = [
                [int(t), round(float(c), 6)]
                for t, c in zip(stamps, closes)
                if c is not None and not math.isnan(c)
            ]
            price = meta.get("regularMarketPrice")
            if price is not None and history:
                # Daily bars are stamped at the session open, so a live price within ~20h belongs to
                # the last bar (update it); only a later price starts a new bar.
                market_time = int(meta.get("regularMarketTime") or 0)
                if market_time - history[-1][0] > 20 * 3600:
                    history.append([market_time, float(price)])
                else:
                    history[-1][1] = float(price)
            return {"meta": meta, "history": history}
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(last_error or "unavailable")


def _pct(a: float, b: float) -> float | None:
    if not b:
        return None
    return round((a / b - 1.0) * 100.0, 3)


def analytics(history: list[list[float]]) -> dict:
    closes = np.array([p[1] for p in history], dtype=float)
    if closes.size < 3:
        return {}
    last = float(closes[-1])

    def back(n: int) -> float | None:
        return _pct(last, float(closes[-1 - n])) if closes.size > n else None

    rets = np.diff(np.log(closes))
    vol20 = float(np.std(rets[-20:]) * math.sqrt(252) * 100) if rets.size >= 10 else None
    high = float(closes.max())
    low = float(closes.min())
    drawdown = _pct(last, high)
    sma50 = float(closes[-50:].mean()) if closes.size >= 50 else None
    sma200 = float(closes[-200:].mean()) if closes.size >= 200 else None
    rsi = None
    if rets.size >= 15:
        delta = np.diff(closes[-15:])
        gain = delta[delta > 0].sum() / 14
        loss = -delta[delta < 0].sum() / 14
        rsi = 100.0 if loss == 0 else float(100 - 100 / (1 + gain / loss))
    chg_1d = back(1)
    chg_5d = back(5)
    chg_1m = back(21)
    chg_3m = back(63)
    if drawdown is not None and drawdown <= -20:
        regime = "bear market"
    elif drawdown is not None and drawdown <= -10:
        regime = "correction"
    elif sma50 and sma200 and last > sma50 > sma200:
        regime = "uptrend"
    elif sma50 and last < sma50:
        regime = "below 50-day average"
    else:
        regime = "range-bound"
    return {
        "last": round(last, 4),
        "chg_1d": chg_1d,
        "chg_5d": chg_5d,
        "chg_1m": chg_1m,
        "chg_3m": chg_3m,
        "chg_1y": _pct(last, float(closes[0])),
        "high_52w": round(high, 4),
        "low_52w": round(low, 4),
        "drawdown_pct": drawdown,
        "vol_20d_ann": round(vol20, 2) if vol20 is not None else None,
        "rsi14": round(rsi, 1) if rsi is not None else None,
        "sma50": round(sma50, 4) if sma50 else None,
        "sma200": round(sma200, 4) if sma200 else None,
        "regime": regime,
        "observations": int(closes.size),
    }


def monte_carlo(history: list[list[float]], horizon: int = 20, paths: int = 3000, seed: int = 7) -> dict:
    """Filtered historical simulation: bootstrap standardized returns (5-day blocks), rescale by EWMA vol."""
    closes = np.array([p[1] for p in history], dtype=float)
    if closes.size < 40:
        return {}
    rets = np.diff(np.log(closes))
    lam = 0.94
    var = np.empty_like(rets)
    var[0] = rets[:20].var() if rets.size >= 20 else rets.var()
    for i in range(1, rets.size):
        var[i] = lam * var[i - 1] + (1 - lam) * rets[i - 1] ** 2
    sigma = np.sqrt(np.maximum(var, 1e-10))
    z = rets / sigma
    z = (z - z.mean()) / (z.std() or 1.0)
    current_sigma = math.sqrt(lam * var[-1] + (1 - lam) * rets[-1] ** 2)
    drift = float(np.clip(rets.mean(), -0.002, 0.002)) * 0.5  # shrink historical drift

    rng = np.random.default_rng(seed)
    block = 5
    n_blocks = math.ceil(horizon / block)
    starts = rng.integers(0, max(1, z.size - block), size=(paths, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(paths, -1)[:, :horizon]
    sim = drift + current_sigma * z[idx]
    log_paths = np.cumsum(sim, axis=1)
    levels = closes[-1] * np.exp(log_paths)
    pct = (levels / closes[-1] - 1.0) * 100.0
    quantiles = np.percentile(levels, [5, 25, 50, 75, 95], axis=0)
    final = pct[:, -1]
    return {
        "horizon_days": horizon,
        "paths": paths,
        "last": float(closes[-1]),
        "p5": quantiles[0].round(4).tolist(),
        "p25": quantiles[1].round(4).tolist(),
        "p50": quantiles[2].round(4).tolist(),
        "p75": quantiles[3].round(4).tolist(),
        "p95": quantiles[4].round(4).tolist(),
        "expected_pct": round(float(final.mean()), 3),
        "median_pct": round(float(np.median(final)), 3),
        "prob_down_5": round(float((final <= -5).mean()), 4),
        "prob_down_10": round(float((final <= -10).mean()), 4),
        "prob_up_5": round(float((final >= 5).mean()), 4),
        "prob_below_today": round(float((final < 0).mean()), 4),
        "daily_vol_pct": round(current_sigma * 100, 3),
    }


def is_down_alert(item: dict) -> str | None:
    a = item.get("analytics") or {}
    if item.get("kind") != "index":
        return None
    if a.get("chg_1d") is not None and a["chg_1d"] <= config.MARKET_DOWN_1D:
        return f"down {a['chg_1d']:.2f}% today"
    if a.get("chg_5d") is not None and a["chg_5d"] <= config.MARKET_DOWN_5D:
        return f"down {a['chg_5d']:.2f}% over 5 sessions"
    if a.get("drawdown_pct") is not None and a["drawdown_pct"] <= config.MARKET_DRAWDOWN:
        return f"{a['drawdown_pct']:.1f}% below its 52-week high ({a.get('regime')})"
    return None


async def refresh_markets(progress=None) -> dict:
    results: list[dict] = []
    sem = asyncio.Semaphore(4)

    async with make_client() as client:
        async def one(spec: dict) -> dict:
            async with sem:
                try:
                    data = await fetch_chart(client, spec["symbol"])
                    hist = data["history"][-260:]
                    stats = analytics(hist)
                    live_change = data["meta"].get("regularMarketChangePercent")
                    if isinstance(live_change, (int, float)):
                        stats["chg_1d"] = round(float(live_change), 3)  # exchange-reported daily change
                    return {
                        **spec,
                        "ok": bool(hist),
                        "error": None if hist else "no history",
                        "currency": data["meta"].get("currency"),
                        "price": stats.get("last"),
                        "change_1d": stats.get("chg_1d"),
                        "analytics": stats,
                        "history": hist,
                    }
                except Exception as exc:  # noqa: BLE001
                    return {**spec, "ok": False, "error": str(exc)[:300], "history": [], "analytics": {}}

        tasks = [asyncio.create_task(one(spec)) for spec in MARKETS]
        for i, fut in enumerate(asyncio.as_completed(tasks), start=1):
            results.append(await fut)
            if progress:
                await progress(i / len(MARKETS), f"{i}/{len(MARKETS)} instruments")

    now = utcnow()
    async with SessionLocal() as session:
        for item in results:
            values = {
                "symbol": item["symbol"],
                "name": item["name"],
                "kind": item["kind"],
                "country": item.get("country"),
                "currency": item.get("currency"),
                "ok": item["ok"],
                "error": item.get("error"),
                "fetched_at": now,
            }
            if item["ok"]:
                values.update(
                    price=item.get("price"),
                    change_1d=item.get("change_1d"),
                    analytics=item["analytics"],
                    history=item["history"],
                )
            stmt = insert(MarketQuote).values(**values)
            update_cols = {k: stmt.excluded[k] for k in values if k != "symbol"}
            await session.execute(stmt.on_conflict_do_update(index_elements=["symbol"], set_=update_cols))
        await session.commit()

    alerts = []
    for item in results:
        if item["ok"]:
            reason = is_down_alert(item)
            if reason and item.get("primary"):
                alerts.append({"symbol": item["symbol"], "name": item["name"], "country": item.get("country"),
                               "reason": reason, "analytics": item["analytics"]})
    return {
        "instruments": len(results),
        "ok": sum(1 for r in results if r["ok"]),
        "failed": [r["symbol"] for r in results if not r["ok"]],
        "alerts": alerts,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


async def load_quotes() -> list[MarketQuote]:
    async with SessionLocal() as session:
        result = await session.execute(select(MarketQuote).order_by(MarketQuote.symbol))
        return list(result.scalars().all())
