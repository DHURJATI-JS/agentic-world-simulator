"""
Vectorised consumer agents for one shard (pure numpy - runs inside a worker process).

Each agent has: region, relative income, price sensitivity, social susceptibility,
an individual contagion threshold, equity exposure, confidence, contagion state
(panic / euphoria), brand awareness and an affinity *memory* per company, and the
company it currently uses. Agents sit on a small-world social network.

Nothing below scripts a crash or a trend: herd behaviour emerges from threshold
contagion on the network, word of mouth, social proof in choices and the feedback
between confidence and the trader markets.
"""
from __future__ import annotations

import numpy as np

DEFAULTS: dict[str, float] = {
    "k_neighbors": 8,
    "rewire_p": 0.08,
    "purchase_every": 7.0,      # an agent is "in market" about once a week
    # confidence dynamics
    "w_social": 0.12,
    "w_region": 0.03,
    "w_global": 0.01,
    "w_news": 0.12,
    "w_wealth": 4.0,
    "reversion": 0.03,
    "noise": 0.03,
    # contagion (SIS threshold model)
    "panic_level": -0.45,
    "euphoria_level": 0.55,
    "contagion_p": 0.25,
    "panic_recovery": 0.08,
    "euphoria_recovery": 0.08,
    "panic_pull": 0.12,
    "euphoria_pull": 0.06,
    # awareness / memory
    "awareness_decay": 0.004,
    "wom_rate": 0.05,
    "ad_effect": 0.30,
    "media_effect": 0.60,
    "aware_threshold": 0.05,
    "affinity_alpha": 0.20,
    "affinity_decay": 0.002,
    "expectation": 0.55,
    # purchase decision
    "buy_base": -0.2,
    "buy_conf": 1.2,
    "buy_conf_essential": 0.35,
    "buy_need": 1.5,
    "beta_quality": 1.5,
    "beta_price": 1.3,
    "beta_aware": 0.6,
    "beta_affinity": 1.2,
    "beta_social": 1.4,
    "beta_rep": 1.0,
}


def small_world(n: int, k: int, p: float, rng: np.random.Generator) -> np.ndarray:
    """Watts-Strogatz style neighbour table (n, k) on a ring of region-sorted agents."""
    k = max(2, min(int(k), max(2, n - 1)))
    half = max(1, k // 2)
    offsets = np.concatenate([np.arange(1, half + 1), -np.arange(1, half + 1)])
    nbrs = (np.arange(n)[:, None] + offsets[None, :]) % n
    rewire = rng.random(nbrs.shape) < p
    nbrs[rewire] = rng.integers(0, n, size=int(rewire.sum()))
    selfloop = nbrs == np.arange(n)[:, None]
    nbrs[selfloop] = (nbrs[selfloop] + 1) % n
    return nbrs.astype(np.int32)


class ConsumerShard:
    def __init__(self, spec: dict) -> None:
        P = dict(DEFAULTS)
        P.update(spec.get("params") or {})
        self.P = P
        rng = np.random.default_rng(int(spec["seed"]))
        self.rng = rng
        R = int(spec["n_regions"])
        C = int(spec["n_companies"])
        self.R, self.C = R, C
        regions = spec["regions"]

        counts = np.array([int(r["count"]) for r in regions], dtype=np.int64)
        index = np.array([int(r["index"]) for r in regions], dtype=np.int64)
        region = np.repeat(index, counts).astype(np.int64)
        n = int(region.size)
        self.n = n
        self.region = region

        income_level = np.ones(R)
        baseline = np.zeros(R)
        equity_level = np.full(R, 0.3)
        for r in regions:
            income_level[r["index"]] = max(0.05, float(r.get("income_level", 1.0)))
            baseline[r["index"]] = float(r.get("baseline_conf", 0.0))
            equity_level[r["index"]] = float(r.get("equity_level", 0.3))

        self.income_rel = rng.lognormal(mean=-0.15, sigma=0.55, size=n).astype(np.float32)
        self.afford = ((income_level[region] ** 0.35) * np.sqrt(self.income_rel)).astype(np.float32)
        self.price_sens = np.clip(
            0.55 - 0.18 * np.log(self.income_rel) + 0.15 * rng.standard_normal(n), 0.05, 1.0
        ).astype(np.float32)
        self.social = rng.beta(2.2, 3.0, n).astype(np.float32)
        self.threshold = rng.uniform(0.30, 0.80, n).astype(np.float32)
        self.equity = np.clip(
            rng.beta(1.3, 5.0, n) * equity_level[region] * (0.6 + 0.4 * np.clip(self.income_rel, 0, 3)), 0, 1
        ).astype(np.float32)
        self.conf = np.clip(baseline[region] + 0.22 * rng.standard_normal(n), -1, 1).astype(np.float32)
        self.panic = np.zeros(n, dtype=bool)
        self.euph = np.zeros(n, dtype=bool)

        init_aw = np.asarray(spec["init_awareness"], dtype=np.float32)  # (C, R)
        self.aw = np.clip(init_aw[:, region].T * rng.uniform(0.4, 1.6, (n, C)), 0, 1).astype(np.float32)
        self.aff = np.zeros((n, C), dtype=np.float32)

        init_share = np.asarray(spec["init_share"], dtype=np.float64)  # (C, R)
        cum = np.cumsum(init_share[:, region].T, axis=1)  # (n, C)
        u = rng.random(n)
        owner = (u[:, None] < cum).argmax(axis=1)
        owner[u >= cum[:, -1]] = -1
        self.owner = owner.astype(np.int16)
        has = np.nonzero(self.owner >= 0)[0]
        if has.size:
            cols = self.owner[has].astype(np.int64)
            self.aw[has, cols] = np.maximum(self.aw[has, cols], 0.6)
            self.aff[has, cols] = 0.2

        self.nbrs = small_world(n, int(P["k_neighbors"]), float(P["rewire_p"]), rng)
        self.essential = bool(spec.get("essential", False))
        self.counts = np.bincount(region, minlength=R).astype(np.float64)

    # ------------------------------------------------------------------
    def step(self, g: dict) -> dict:
        P, rng = self.P, self.rng
        n, C, R = self.n, self.C, self.R
        reg = self.region
        nbrs = self.nbrs

        # 1. Confidence: social averaging, regional/global field, news, wealth effect.
        conf = self.conf
        nb_mean = conf[nbrs].mean(axis=1)
        news = np.asarray(g["news_shock"], np.float32)[reg]
        mret = np.asarray(g["market_return"], np.float32)[reg]
        rfield = np.asarray(g["region_conf"], np.float32)[reg]
        base = np.asarray(g["baseline_conf"], np.float32)[reg]
        drift = (
            P["w_social"] * self.social * (nb_mean - conf)
            + P["w_region"] * (rfield - conf)
            + P["w_global"] * (float(g["global_conf"]) - conf)
            + P["w_news"] * news
            + P["w_wealth"] * self.equity * mret
            - P["reversion"] * (conf - base)
            + P["noise"] * rng.standard_normal(n).astype(np.float32)
        )
        new_conf = conf + drift

        # 2. Threshold contagion (herding): panic and euphoria spread through the network.
        nb_panic = self.panic[nbrs].mean(axis=1)
        nb_euph = self.euph[nbrs].mean(axis=1)
        thr = self.threshold * (1.25 - self.social)
        roll = rng.random(n)
        enter_panic = (~self.panic) & (roll < P["contagion_p"]) & (
            (nb_panic > thr) | ((new_conf < P["panic_level"]) & (drift < 0))
        )
        enter_euph = (~self.euph) & (~enter_panic) & (roll < P["contagion_p"]) & (
            (nb_euph > thr) | ((new_conf > P["euphoria_level"]) & (drift > 0))
        )
        recover = rng.random(n)
        self.panic = (self.panic | enter_panic) & ~(recover < P["panic_recovery"])
        self.euph = ((self.euph & ~enter_panic) | enter_euph) & ~(recover < P["euphoria_recovery"])
        new_conf += P["panic_pull"] * self.panic * (-0.85 - new_conf)
        new_conf += P["euphoria_pull"] * self.euph * (0.85 - new_conf)
        np.clip(new_conf, -1.0, 1.0, out=new_conf)
        self.conf = new_conf.astype(np.float32)

        # 3. Awareness: memory decay, advertising, media coverage, word of mouth.
        aw = self.aw
        aw *= 1.0 - P["awareness_decay"]
        reach = np.asarray(g["ad_reach"], np.float32)[:, reg].T
        hits = rng.random((n, C), dtype=np.float32) < reach
        aw += hits * (P["ad_effect"] * (1.0 - aw))
        media = np.asarray(g["media"], np.float32)
        if media.any():
            aw += media[:, reg].T * P["media_effect"] * rng.random((n, C), dtype=np.float32) * (1.0 - aw)
        owners_nb = self.owner[nbrs]
        own_nb = np.empty((n, C), dtype=np.float32)
        for c in range(C):
            own_nb[:, c] = (owners_nb == c).mean(axis=1)
        aw += P["wom_rate"] * own_nb * (1.0 - aw)
        np.clip(aw, 0.0, 1.0, out=aw)

        # 4. Purchases: who is in market, do they buy, and from whom (multinomial logit).
        buyers = np.zeros(C * R)
        attempts = np.zeros(C * R)
        fails = np.zeros(C * R)
        conform = 0
        choices = 0
        active = np.nonzero(rng.random(n) < 1.0 / P["purchase_every"])[0]
        if active.size:
            need = np.asarray(g["need"], np.float32)[reg[active]]
            w_conf = P["buy_conf_essential"] if self.essential else P["buy_conf"]
            logits = P["buy_base"] + w_conf * self.conf[active] + P["buy_need"] * (need - 1.0)
            p_buy = 1.0 / (1.0 + np.exp(-logits))
            active = active[rng.random(active.size) < p_buy]
        if active.size:
            r = reg[active]
            price = np.asarray(g["price"], np.float32)
            quality = np.asarray(g["quality"], np.float32)
            rep = np.asarray(g["reputation"], np.float32)[:, r].T
            avail = np.asarray(g["available"], bool)[:, r].T
            aw_i = aw[active]
            utility = (
                P["beta_quality"] * quality[None, :]
                - P["beta_price"] * self.price_sens[active, None] * (price[None, :] / self.afford[active, None])
                + P["beta_aware"] * np.log(aw_i + 0.02)
                + P["beta_affinity"] * self.aff[active]
                + P["beta_social"] * self.social[active, None] * own_nb[active]
                + P["beta_rep"] * rep
                + rng.gumbel(size=(active.size, C)).astype(np.float32)
            )
            utility[(~avail) | (aw_i < P["aware_threshold"])] = -np.inf
            ok = np.isfinite(utility).any(axis=1)
            active, r, utility = active[ok], r[ok], utility[ok]
            if active.size:
                choice = utility.argmax(axis=1)
                stockout = np.asarray(g["stockout"], np.float32)[choice, r]
                failed = rng.random(active.size) < stockout
                flat = choice * R + r
                attempts = np.bincount(flat, minlength=C * R).astype(float)
                fails = np.bincount(flat[failed], minlength=C * R).astype(float)
                buyers = attempts - fails

                # experience -> affinity memory (frustration on stock-outs)
                rel_price = price[choice] / self.afford[active]
                experience = (
                    quality[choice]
                    - P["expectation"]
                    - 0.35 * self.price_sens[active] * (rel_price - 1.0)
                    + 0.25 * rng.standard_normal(active.size)
                ).astype(np.float32)
                experience[failed] = -0.6
                a = P["affinity_alpha"]
                self.aff[active, choice] = (1 - a) * self.aff[active, choice] + a * experience
                good = ~failed
                self.owner[active[good]] = choice[good].astype(np.int16)

                # conformity: did the agent pick what most of its neighbours use?
                nb_pick = own_nb[active]
                has_nb = nb_pick.max(axis=1) > 0
                conform = int(((nb_pick.argmax(axis=1) == choice) & has_nb).sum())
                choices = int(has_nb.sum())
        self.aff *= 1.0 - P["affinity_decay"]

        # 5. Aggregates per region (summed across shards by the coordinator).
        conf64 = self.conf.astype(np.float64)
        own_mask = self.owner >= 0
        owners = np.bincount(
            self.owner[own_mask].astype(np.int64) * R + reg[own_mask], minlength=C * R
        ).reshape(C, R)
        return {
            "counts": self.counts,
            "conf_sum": np.bincount(reg, weights=conf64, minlength=R),
            "conf_sq": np.bincount(reg, weights=conf64**2, minlength=R),
            "panic": np.bincount(reg, weights=self.panic.astype(np.float64), minlength=R),
            "euph": np.bincount(reg, weights=self.euph.astype(np.float64), minlength=R),
            "aw_sum": np.stack([np.bincount(reg, weights=aw[:, c], minlength=R) for c in range(C)]),
            "aff_sum": np.stack([np.bincount(reg, weights=self.aff[:, c], minlength=R) for c in range(C)]),
            "owners": owners.astype(np.float64),
            "buyers": buyers.reshape(C, R),
            "attempts": attempts.reshape(C, R),
            "fails": fails.reshape(C, R),
            "conform": conform,
            "choices": choices,
        }
