"""Detect emergent patterns in simulation output (nothing here is scripted into the model)."""
from __future__ import annotations

import math

import numpy as np


def _episodes(mask: np.ndarray, min_len: int, gap: int = 3) -> list[tuple[int, int]]:
    """Runs of True at least min_len long; runs separated by <= gap ticks are merged into one episode."""
    runs, start = [], None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(mask) - 1))
    merged: list[list[int]] = []
    for s, e in runs:
        if merged and s - merged[-1][1] - 1 <= gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s + 1 >= min_len]


def _sev(value: float, medium: float, high: float) -> str:
    v = abs(value)
    return "high" if v >= high else ("medium" if v >= medium else "low")


def returns(prices: np.ndarray) -> np.ndarray:
    prices = np.asarray(prices, dtype=float)
    return np.diff(np.log(np.maximum(prices, 1e-12)))


def acf(x: np.ndarray, lag: int) -> float:
    x = np.asarray(x, dtype=float)
    if x.size <= lag + 2:
        return 0.0
    a, b = x[:-lag] - x.mean(), x[lag:] - x.mean()
    denom = math.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / denom) if denom > 0 else 0.0


def excess_kurtosis(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    if x.size < 10 or x.std() == 0:
        return 0.0
    z = (x - x.mean()) / x.std()
    return float((z**4).mean() - 3.0)


def sadf(prices: np.ndarray, min_window: int = 30) -> float:
    """Simplified supremum ADF (right-tailed) statistic over forward-expanding windows."""
    y = np.log(np.maximum(np.asarray(prices, dtype=float), 1e-12))
    best = -np.inf
    if y.size < min_window + 2:
        return float("nan")
    for end in range(min_window, y.size):
        yy = y[: end + 1]
        dy = np.diff(yy)
        x = yy[:-1]
        X = np.column_stack([np.ones_like(x), x])
        try:
            coef, *_ = np.linalg.lstsq(X, dy, rcond=None)
        except np.linalg.LinAlgError:
            continue
        resid = dy - X @ coef
        dof = max(1, dy.size - 2)
        s2 = (resid @ resid) / dof
        try:
            cov = s2 * np.linalg.inv(X.T @ X)
        except np.linalg.LinAlgError:
            continue
        se = math.sqrt(max(cov[1, 1], 1e-18))
        best = max(best, coef[1] / se)
    return float(best)


def market_patterns(name: str, prices: list[float], fundamentals: list[float],
                    frac_c: list[float], xbar: list[float]) -> list[dict]:
    out: list[dict] = []
    p = np.asarray(prices, dtype=float)
    f = np.asarray(fundamentals, dtype=float)
    if p.size < 20:
        return out
    r = returns(p)

    # Crashes: >=10% fall from a recent (20-tick) peak.
    window = 20
    peaks = np.array([p[max(0, i - window): i + 1].max() for i in range(p.size)])
    dd = p / peaks - 1.0
    for s, e in _episodes(dd <= -0.10, 1):
        trough = s + int(np.argmin(dd[s: e + 1]))
        depth = float(dd[trough]) * 100
        out.append({
            "type": "crash", "label": f"Crash in {name}", "severity": _sev(depth, 15, 25),
            "start": s, "end": e, "metric": {"depth_pct": round(depth, 2)},
            "description": f"{name} fell {abs(depth):.1f}% from its recent peak (trough at tick {trough}).",
        })

    # Bubbles: price persistently above fundamental value.
    dev = p / np.maximum(f, 1e-9) - 1.0
    for s, e in _episodes(dev > 0.12, 8):
        peak = float(dev[s: e + 1].max()) * 100
        out.append({
            "type": "bubble", "label": f"Bubble in {name}", "severity": _sev(peak, 20, 35),
            "start": s, "end": e, "metric": {"peak_overvaluation_pct": round(peak, 1)},
            "description": f"{name} traded up to {peak:.1f}% above fundamental value for {e - s + 1} ticks.",
        })
    stat = sadf(p)
    if stat == stat and stat > 1.5:
        out.append({
            "type": "explosive", "label": f"Explosive price dynamics in {name}", "severity": "medium",
            "start": 0, "end": int(p.size - 1), "metric": {"sadf": round(stat, 2), "approx_cv95": 1.5},
            "description": f"Right-tailed unit-root test (SADF={stat:.2f} > ~1.5) suggests a speculative episode.",
        })

    # Herding among traders: chartists lining up on one side.
    xb = np.asarray(xbar, dtype=float)
    fc = np.asarray(frac_c, dtype=float)
    for s, e in _episodes(np.abs(xb) > 0.6, 5):
        side = "bullish" if xb[s: e + 1].mean() > 0 else "bearish"
        out.append({
            "type": "herding", "label": f"Trader herding in {name}", "severity": _sev(float(np.abs(xb[s:e+1]).max()), 0.75, 0.9),
            "start": s, "end": e,
            "metric": {"max_consensus": round(float(np.abs(xb[s: e + 1]).max()), 2),
                       "chartist_share": round(float(fc[s: e + 1].mean()), 2)},
            "description": f"{side.title()} herd: up to {np.abs(xb[s:e+1]).max()*100:.0f}% net consensus among "
                           f"trend-followers for {e - s + 1} ticks.",
        })

    if r.size >= 40:
        vc = float(np.mean([acf(np.abs(r), lag) for lag in range(1, 6)]))
        if vc > 0.1:
            out.append({
                "type": "volatility_clustering", "label": f"Volatility clustering in {name}", "severity": "low",
                "start": 0, "end": int(p.size - 1), "metric": {"acf_abs_returns": round(vc, 3)},
                "description": "Calm and turbulent periods bunch together (|returns| are autocorrelated).",
            })
        k = excess_kurtosis(r)
        if k > 1.0:
            out.append({
                "type": "fat_tails", "label": f"Fat tails in {name}", "severity": "low",
                "start": 0, "end": int(p.size - 1), "metric": {"excess_kurtosis": round(k, 2)},
                "description": "Extreme moves are far more frequent than a normal distribution predicts.",
            })
    return out


def consumer_patterns(region_names: list[str], panic: dict[str, list[float]],
                      conf: dict[str, list[float]], conformity: list[float]) -> list[dict]:
    out: list[dict] = []
    for name in region_names:
        series = np.asarray(panic.get(name) or [], dtype=float)
        if series.size < 5:
            continue
        for s, e in _episodes(series > 0.2, 3):
            peak = float(series[s: e + 1].max())
            out.append({
                "type": "panic_cascade", "label": f"Consumer panic cascade in {name}",
                "severity": _sev(peak, 0.35, 0.55), "start": s, "end": e,
                "metric": {"peak_share_panicking": round(peak, 3)},
                "description": f"Fear spread through social ties: up to {peak*100:.0f}% of consumers in {name} "
                               f"were in panic mode, cutting spending.",
            })
    # Sentiment regime shift (CUSUM-style largest mean shift in the average across regions)
    if conf:
        mat = np.array([np.asarray(v, dtype=float) for v in conf.values() if len(v)], dtype=float)
        if mat.size and mat.shape[1] >= 30:
            avg = mat.mean(axis=0)
            best_t, best_gap = None, 0.0
            for t in range(10, avg.size - 10):
                gap = avg[t:].mean() - avg[:t].mean()
                if abs(gap) > abs(best_gap):
                    best_t, best_gap = t, gap
            if best_t is not None and abs(best_gap) > 0.15:
                out.append({
                    "type": "regime_shift", "label": "Consumer sentiment regime shift",
                    "severity": _sev(best_gap, 0.25, 0.4), "start": best_t, "end": int(avg.size - 1),
                    "metric": {"mean_shift": round(best_gap, 3)},
                    "description": f"Average consumer confidence shifted by {best_gap:+.2f} around tick {best_t} "
                                   "and stayed there.",
                })
    conf_arr = np.asarray(conformity, dtype=float)
    if conf_arr.size >= 10 and conf_arr.mean() > 0.55:
        out.append({
            "type": "social_proof", "label": "Strong social proof in purchases", "severity": "low",
            "start": 0, "end": int(conf_arr.size - 1), "metric": {"avg_conformity": round(float(conf_arr.mean()), 3)},
            "description": f"{conf_arr.mean()*100:.0f}% of purchases matched what most of the buyer's friends use.",
        })
    return out


def company_patterns(company_names: list[str], shares: dict[str, list[float]],
                     prices: dict[str, list[float]], startup: str | None) -> list[dict]:
    out: list[dict] = []
    for name in company_names:
        s = np.asarray(shares.get(name) or [], dtype=float)
        if s.size < 30:
            continue
        smooth = np.convolve(s, np.ones(7) / 7, mode="valid")
        if smooth.size < 10:
            continue
        growth = np.diff(smooth)
        t = int(np.argmax(growth))
        start_share, end_share = float(smooth[0]), float(smooth[-1])
        if end_share > 0.03 and end_share > 2.0 * max(start_share, 1e-4) and growth[t] > 0:
            out.append({
                "type": "tipping_point", "label": f"Adoption tipping point: {name}",
                "severity": "medium" if name == startup else "low", "start": t, "end": int(s.size - 1),
                "metric": {"start_share": round(start_share, 4), "end_share": round(end_share, 4)},
                "description": f"{name} adoption accelerated around tick {t} as word of mouth took over "
                               f"({start_share*100:.1f}% -> {end_share*100:.1f}%).",
            })
        elif end_share < 0.6 * start_share and start_share > 0.02:
            out.append({
                "type": "decline", "label": f"Customer flight from {name}", "severity": "medium",
                "start": 0, "end": int(s.size - 1),
                "metric": {"start_share": round(start_share, 4), "end_share": round(end_share, 4)},
                "description": f"{name} lost {(1 - end_share / start_share)*100:.0f}% of its customer share.",
            })
    finals = np.array([(shares.get(n) or [0])[-1] for n in company_names], dtype=float)
    total = finals.sum()
    if total > 0:
        norm = finals / total
        hhi = float((norm**2).sum())
        if hhi > 0.45:
            leader = company_names[int(np.argmax(norm))]
            out.append({
                "type": "winner_take_all", "label": f"Market concentrating around {leader}", "severity": "medium",
                "start": 0, "end": len(next(iter(shares.values()))) - 1, "metric": {"hhi": round(hhi, 3)},
                "description": f"Herfindahl index {hhi:.2f}: {leader} holds {norm.max()*100:.0f}% of buyers.",
            })
    cutters = []
    for name, series in prices.items():
        arr = np.asarray(series, dtype=float)
        if arr.size > 10 and arr[-1] < 0.88 * arr[0]:
            cutters.append(name)
    if len(cutters) >= 2:
        out.append({
            "type": "price_war", "label": "Price war", "severity": "medium",
            "start": 0, "end": len(next(iter(prices.values()))) - 1, "metric": {"companies": cutters},
            "description": f"{', '.join(cutters)} each cut prices by more than 12% while learning from profits.",
        })
    return out
