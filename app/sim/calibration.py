"""
Calibrate the trader-market model against a real index (method of simulated moments).

Target moments from real daily log returns: volatility, excess kurtosis (fat tails),
mean autocorrelation of |returns| at lags 1-5 (volatility clustering) and lag-1
autocorrelation of returns. Random search + local refinement over the behavioural
parameters; the best set is stored and used for that market in every simulation.
"""
from __future__ import annotations

import numpy as np

from app.sim.patterns import acf, excess_kurtosis
from app.sim.traders import simulate_returns

SPACE: dict[str, tuple[float, float]] = {
    "gamma_f": (0.02, 0.10),
    "gamma_c": (0.002, 0.015),
    "noise_f": (0.002, 0.008),
    "noise_c": (0.010, 0.035),
    "a1": (0.6, 1.3),
    "a2": (0.0, 1.0),
    "alpha0": (-1.2, 0.2),
    "alpha_n": (0.3, 1.5),
    "alpha_p": (0.3, 2.5),
    "sigma_f": (0.002, 0.010),
}


def moments(returns: np.ndarray) -> dict:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    return {
        "std": float(r.std()),
        "kurt": float(excess_kurtosis(r)),
        "acf_abs": float(np.mean([acf(np.abs(r), lag) for lag in range(1, 6)])),
        "acf1": float(acf(r, 1)),
    }


def distance(sim: dict, target: dict) -> float:
    std_scale = max(target["std"], 1e-4)
    kurt_scale = max(1.0, abs(target["kurt"]))
    return float(
        ((sim["std"] - target["std"]) / std_scale) ** 2 * 1.0
        + ((sim["kurt"] - target["kurt"]) / kurt_scale) ** 2 * 0.3
        + ((sim["acf_abs"] - target["acf_abs"]) / 0.1) ** 2 * 1.0
        + ((sim["acf1"] - target["acf1"]) / 0.1) ** 2 * 0.5
    )


def _evaluate(params: dict, ticks: int, seeds: tuple[int, ...]) -> tuple[float, dict]:
    sims = [moments(simulate_returns(params, ticks, seed)) for seed in seeds]
    avg = {k: float(np.mean([s[k] for s in sims])) for k in sims[0]}
    return avg, sims  # type: ignore[return-value]


def calibrate(real_closes: list[float], n_random: int = 40, n_refine: int = 16,
              seeds: tuple[int, ...] = (11, 23), rng_seed: int = 5) -> dict:
    closes = np.asarray(real_closes, dtype=float)
    closes = closes[np.isfinite(closes) & (closes > 0)]
    if closes.size < 60:
        raise ValueError("need at least 60 daily closes to calibrate")
    real = np.diff(np.log(closes))
    target = moments(real)
    ticks = int(min(500, max(120, real.size)))
    rng = np.random.default_rng(rng_seed)

    def sample() -> dict:
        return {k: float(rng.uniform(lo, hi)) for k, (lo, hi) in SPACE.items()}

    best_params, best_loss, best_sim = None, np.inf, None
    evaluated = 0
    for _ in range(n_random):
        params = sample()
        sim, _ = _evaluate(params, ticks, seeds)
        loss = distance(sim, target)
        evaluated += 1
        if loss < best_loss:
            best_params, best_loss, best_sim = params, loss, sim
    for _ in range(n_refine):
        params = {}
        for k, (lo, hi) in SPACE.items():
            params[k] = float(np.clip(best_params[k] + rng.normal(0, 0.1 * (hi - lo)), lo, hi))
        sim, _ = _evaluate(params, ticks, seeds)
        loss = distance(sim, target)
        evaluated += 1
        if loss < best_loss:
            best_params, best_loss, best_sim = params, loss, sim
    return {
        "params": {k: round(v, 6) for k, v in best_params.items()},
        "loss": round(float(best_loss), 5),
        "target": {k: round(v, 6) for k, v in target.items()},
        "sim": {k: round(v, 6) for k, v in best_sim.items()},
        "observations": int(real.size),
        "evaluated": evaluated,
    }
