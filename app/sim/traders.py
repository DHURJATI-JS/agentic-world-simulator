"""
Heterogeneous-agent stock market (one per region with a real index).

Fundamentalists trade the gap to a news-driven fundamental value; chartists hold
optimistic/pessimistic opinions that spread by herding and trend-chasing
(Lux-Marchesi style) and imitation (Kirman). Traders switch strategy through a
herding + mispricing + profitability index (Franke-Westerhoff / Brock-Hommes), and
chartist demand is noisier than fundamentalist demand. Calm and turbulent regimes,
bubbles, crashes, fat tails and volatility clustering emerge from these interactions.
"""
from __future__ import annotations

import math

import numpy as np

DEFAULTS: dict[str, float] = {
    "n_traders": 20000,
    "frac_chartist": 0.30,   # initial share of chartists
    "gamma_f": 0.05,         # fundamentalist reaction to the value gap
    "arb_convexity": 12.0,   # arbitrage pressure grows with large mispricings
    "gamma_c": 0.006,        # chartist demand per unit of net opinion
    "lam": 1.0,              # market-maker price impact
    "noise_f": 0.004,        # demand noise of fundamentalists (daily)
    "noise_c": 0.026,        # demand noise of chartists (daily) - drives volatility regimes
    "nu": 0.06,              # opinion revision speed
    "a1": 1.05,              # herding strength in opinions
    "a2": 0.5,               # trend chasing in opinions
    "trend_alpha": 0.2,
    "switch_nu": 0.05,       # strategy revision speed
    "alpha0": -0.8,          # predisposition (negative favours chartism)
    "alpha_n": 1.0,          # herding in strategy choice
    "alpha_p": 1.5,          # mispricing pushes traders to fundamentalism
    "beta": 20.0,            # profitability in strategy choice
    "memory": 0.92,
    "herd_copy": 0.02,       # imitation of a random chartist
    "mu_f": 0.0002,          # fundamental drift
    "sigma_f": 0.005,        # fundamental news volatility
    "sent_coef": 0.6,        # consumer sentiment -> chartist mood
}


class TraderMarket:
    def __init__(self, params: dict | None, p0: float, seed: int) -> None:
        P = dict(DEFAULTS)
        P.update({k: v for k, v in (params or {}).items() if k in DEFAULTS})
        self.P = P
        self.rng = np.random.default_rng(seed)
        self.n = int(P["n_traders"])
        self.logp = math.log(max(p0, 1e-6))
        self.logf = self.logp
        self.is_c = self.rng.random(self.n) < P["frac_chartist"]
        self.op = np.where(self.rng.random(self.n) < 0.5, 1, -1).astype(np.int8)
        self.trend = 0.0
        self.u_f = 0.0
        self.u_c = 0.0
        self.prev_gap = 0.0
        self.prev_xbar = 0.0

    @property
    def price(self) -> float:
        return math.exp(self.logp)

    @property
    def fundamental(self) -> float:
        return math.exp(self.logf)

    def step(self, fund_shock: float = 0.0, sentiment: float = 0.0, opinion_push: float = 0.0) -> dict:
        P, rng = self.P, self.rng
        self.logf += P["mu_f"] + P["sigma_f"] * rng.standard_normal() + fund_shock

        mask = self.is_c
        n_c = int(mask.sum())
        frac_c = n_c / self.n
        ops = self.op[mask]

        # Opinion dynamics among chartists: herding, trend chasing, sentiment, shocks.
        xbar = float(ops.mean()) if n_c else 0.0
        u = P["a1"] * xbar + P["a2"] * self.trend / 0.01 + P["sent_coef"] * sentiment + opinion_push
        u = max(-4.0, min(4.0, u))
        p_up = min(1.0, P["nu"] * math.exp(u))
        p_down = min(1.0, P["nu"] * math.exp(-u))
        if n_c:
            roll = rng.random(n_c)
            new_ops = ops.copy()
            new_ops[(ops < 0) & (roll < p_up)] = 1
            new_ops[(ops > 0) & (roll < p_down)] = -1
            copy = rng.random(n_c) < P["herd_copy"]
            if copy.any():
                new_ops[copy] = ops[rng.integers(0, n_c, int(copy.sum()))]
            self.op[mask] = new_ops
            xbar = float(new_ops.mean())

        # Price formation: market maker reacts to excess demand; chartists trade noisier.
        gap = self.logf - self.logp
        demand_f = P["gamma_f"] * gap * (1.0 + P["arb_convexity"] * gap * gap)
        excess = (1.0 - frac_c) * demand_f + frac_c * P["gamma_c"] * xbar
        noise_sd = math.sqrt(((1.0 - frac_c) * P["noise_f"]) ** 2 + (frac_c * P["noise_c"]) ** 2)
        ret = P["lam"] * excess + noise_sd * rng.standard_normal()
        ret = max(-0.25, min(0.25, ret))
        self.logp += ret
        self.trend = (1 - P["trend_alpha"]) * self.trend + P["trend_alpha"] * ret

        # Strategy switching: herding + mispricing + realised profitability.
        pi_f = ret * (1.0 if self.prev_gap > 0 else -1.0 if self.prev_gap < 0 else 0.0)
        pi_c = ret * self.prev_xbar
        self.u_f = P["memory"] * self.u_f + pi_f
        self.u_c = P["memory"] * self.u_c + pi_c
        attract_f = (
            P["alpha0"]
            + P["alpha_n"] * ((1.0 - frac_c) - frac_c)
            + P["alpha_p"] * (gap / 0.05) ** 2
            + P["beta"] * (self.u_f - self.u_c)
        )
        attract_f = max(-6.0, min(6.0, attract_f))
        p_to_f = min(1.0, P["switch_nu"] * math.exp(attract_f))
        p_to_c = min(1.0, P["switch_nu"] * math.exp(-attract_f))
        roll = rng.random(self.n)
        to_f = mask & (roll < p_to_f)
        to_c = (~mask) & (roll < p_to_c)
        self.is_c = (mask & ~to_f) | to_c

        self.prev_gap = gap
        self.prev_xbar = xbar
        return {"ret": ret, "price": self.price, "fundamental": self.fundamental, "frac_c": frac_c, "xbar": xbar}


def simulate_returns(params: dict, ticks: int, seed: int, n_traders: int = 3000) -> np.ndarray:
    """Plain market run (no shocks) used by calibration."""
    p = dict(params)
    p["n_traders"] = n_traders
    market = TraderMarket(p, 100.0, seed)
    return np.array([market.step()["ret"] for _ in range(ticks)])
