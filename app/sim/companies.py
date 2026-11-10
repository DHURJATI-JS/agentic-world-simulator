"""
Company agents (the startup and its competitors). They set price and marketing,
serve demand within capacity, build or lose reputation, run campaigns, and the
adaptive ones *learn* their pricing/marketing with an epsilon-greedy bandit on profit.
"""
from __future__ import annotations

import numpy as np

ACTIONS = [(pm, mm) for pm in (0.94, 1.0, 1.06) for mm in (0.85, 1.0, 1.2)]
MARKETING_COST = 0.0015   # money units per agent per tick at marketing level 1.0
BASE_REACH = 0.004        # ad exposure probability per tick at level 1.0 (uniform allocation)


class CompanyAgent:
    def __init__(self, spec: dict, n_regions: int, region_weights: np.ndarray, n_agents: int, seed: int) -> None:
        self.name = spec["name"]
        self.is_startup = bool(spec.get("is_startup"))
        self.price = float(spec.get("price", 1.0))
        self.base_price = self.price
        self.quality = float(spec.get("quality", 0.5))
        self.unit_cost = float(spec.get("unit_cost", 0.55))
        self.marketing = float(spec.get("marketing", 1.0))
        self.base_marketing = self.marketing
        alloc = np.asarray(spec.get("alloc") or np.ones(n_regions), dtype=float)
        self.alloc = alloc / alloc.sum() if alloc.sum() > 0 else np.ones(n_regions) / n_regions
        self.available = np.asarray(spec.get("available") or [True] * n_regions, dtype=bool)
        self.capacity = spec.get("capacity")  # purchases per tick, None = digital / unlimited
        self.capacity_factor = 1.0
        self.supply_regions = set(spec.get("supply_regions") or [])
        self.adaptive = bool(spec.get("adaptive", False))
        self.reputation = np.zeros(n_regions)
        self.region_weights = region_weights
        self.n_agents = n_agents
        self.cash = 0.0
        self.stockout = np.zeros(n_regions)
        self.campaign_cost_tick = 0.0
        self.marketing_boost = np.ones(n_regions)
        self.rng = np.random.default_rng(seed)
        # learning state
        self.q = np.zeros(len(ACTIONS))
        self.q_n = np.zeros(len(ACTIONS))
        self.action = 4  # (1.0, 1.0)
        self.window_profit: list[float] = []
        self.review_every = int(spec.get("review_every", 14))
        self.epsilon = 0.25
        self.revenue_total = 0.0
        self.profit_total = 0.0
        self.units_total = 0.0

    def ad_reach(self) -> np.ndarray:
        per_region = self.marketing * self.alloc * self.marketing_boost / np.maximum(self.region_weights, 1e-6)
        reach = 1.0 - np.exp(-BASE_REACH * per_region)
        return np.where(self.available, reach, 0.0)

    def settle(self, buyers: np.ndarray, attempts: np.ndarray, aff_mean: np.ndarray, tick: int) -> dict:
        units = float(buyers.sum())
        revenue = units * self.price
        marketing_cost = self.marketing * float((self.alloc * self.marketing_boost).sum()) * self.n_agents * MARKETING_COST
        cost = units * self.unit_cost + marketing_cost + self.campaign_cost_tick
        profit = revenue - cost
        self.cash += profit
        self.revenue_total += revenue
        self.profit_total += profit
        self.units_total += units

        if self.capacity is not None:
            demand = float(attempts.sum())
            cap = float(self.capacity) * self.capacity_factor
            level = max(0.0, 1.0 - cap / demand) if demand > cap > 0 else (1.0 if cap <= 0 and demand > 0 else 0.0)
            self.stockout[:] = level
        # reputation follows customers' remembered experience
        self.reputation = 0.97 * self.reputation + 0.03 * np.clip(aff_mean * 2.0, -1.0, 1.0)
        np.clip(self.reputation, -1.0, 1.0, out=self.reputation)

        if self.adaptive:
            self.window_profit.append(profit)
            if (tick + 1) % self.review_every == 0:
                self._learn()
        return {"units": units, "revenue": revenue, "profit": profit}

    def _learn(self) -> None:
        # fixed scale keeps rewards comparable across windows; constant step size because
        # competitors keep changing the environment (non-stationary bandit)
        scale = max(1.0, self.n_agents * 0.002)
        reward = float(np.mean(self.window_profit)) / scale
        a = self.action
        self.q_n[a] += 1
        step = max(0.3, 1.0 / self.q_n[a])
        self.q[a] += step * (reward - self.q[a])
        self.window_profit = []
        if self.rng.random() < self.epsilon:
            self.action = int(self.rng.integers(len(ACTIONS)))
        else:
            self.action = int(np.argmax(self.q + 1e-6 * self.rng.random(len(ACTIONS))))
        self.epsilon = max(0.05, self.epsilon * 0.9)
        pm, mm = ACTIONS[self.action]
        self.price = float(np.clip(self.price * pm, 0.5 * self.base_price, 2.0 * self.base_price))
        self.marketing = float(np.clip(self.marketing * mm, 0.2 * self.base_marketing, 4.0 * self.base_marketing))
