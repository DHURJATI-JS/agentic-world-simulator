"""
Distributed simulation engine (bulk-synchronous parallel).

Consumers are sharded across worker processes (each keeps its agents in memory).
Every tick the coordinator: applies scenario events -> broadcasts global state ->
shards step their agents in parallel -> aggregates per region -> companies settle
and learn -> regional trader markets step, coupled to consumer confidence ->
results are recorded. Emergent patterns are detected afterwards.
"""
from __future__ import annotations

import math
import multiprocessing as mp
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from app.sim import patterns as pat
from app.sim.companies import MARKETING_COST, CompanyAgent
from app.sim.consumers import ConsumerShard
from app.sim.traders import TraderMarket

Progress = Callable[[float, str], None]


class SimCancelled(Exception):
    pass


@dataclass
class SimConfig:
    regions: list[dict]
    companies: list[dict]
    events: list[dict] = field(default_factory=list)
    consumers: int = 200_000
    ticks: int = 180
    shards: int = 0
    seed: int = 42
    essential: bool = False
    use_processes: bool = True
    traders_per_market: int = 20_000
    consumer_params: dict = field(default_factory=dict)
    name: str = "simulation"


# ---------------------------------------------------------------------------
# Shard backends
# ---------------------------------------------------------------------------
def _shard_worker(conn, spec: dict) -> None:
    try:
        shard = ConsumerShard(spec)
        conn.send(("ready", shard.n))
    except BaseException:  # noqa: BLE001
        conn.send(("error", traceback.format_exc()))
        conn.close()
        return
    while True:
        try:
            kind, payload = conn.recv()
        except (EOFError, OSError):
            break
        if kind == "step":
            try:
                conn.send(("ok", shard.step(payload)))
            except BaseException:  # noqa: BLE001
                conn.send(("error", traceback.format_exc()))
                break
        elif kind == "stop":
            break
    conn.close()


class LocalBackend:
    name = "in-process"

    def __init__(self, specs: list[dict]) -> None:
        self.specs = specs
        self.shards: list[ConsumerShard] = []

    def start(self) -> None:
        self.shards = [ConsumerShard(s) for s in self.specs]

    def step(self, g: dict) -> list[dict]:
        return [s.step(g) for s in self.shards]

    def stop(self) -> None:
        self.shards = []


class ProcessBackend:
    name = "multi-process"

    def __init__(self, specs: list[dict], step_timeout: float = 600) -> None:
        self.specs = specs
        self.step_timeout = step_timeout
        self.procs: list = []
        self.conns: list = []

    def start(self) -> None:
        ctx = mp.get_context("spawn")
        for spec in self.specs:
            parent, child = ctx.Pipe()
            proc = ctx.Process(target=_shard_worker, args=(child, spec), daemon=True)
            proc.start()
            child.close()
            self.procs.append(proc)
            self.conns.append(parent)
        for conn in self.conns:
            self._recv(conn, 300)

    def _recv(self, conn, timeout: float):
        if not conn.poll(timeout):
            raise TimeoutError("a simulation shard did not respond in time")
        kind, payload = conn.recv()
        if kind == "error":
            raise RuntimeError(f"simulation shard failed:\n{payload}")
        return payload

    def step(self, g: dict) -> list[dict]:
        for conn in self.conns:
            conn.send(("step", g))
        return [self._recv(conn, self.step_timeout) for conn in self.conns]

    def stop(self) -> None:
        for conn in self.conns:
            try:
                conn.send(("stop", None))
            except Exception:  # noqa: BLE001
                pass
        for proc in self.procs:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
        for conn in self.conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        self.procs, self.conns = [], []


def plan_shards(cfg: SimConfig) -> tuple[list[dict], np.ndarray, int]:
    R, C = len(cfg.regions), len(cfg.companies)
    weights = np.array([max(1e-6, float(r.get("weight", 1.0))) for r in cfg.regions])
    weights = weights / weights.sum()
    min_per_region = max(300, int(cfg.consumers * 0.01))
    counts = np.maximum(min_per_region, np.round(weights * cfg.consumers)).astype(np.int64)
    shards = cfg.shards or min(8, max(1, (os.cpu_count() or 2) - 1))
    shards = int(max(1, min(shards, max(1, counts.sum() // 25_000))))
    init_aw = [[float(v) for v in comp["init_awareness"]] for comp in cfg.companies]
    init_share = [[float(v) for v in comp["init_share"]] for comp in cfg.companies]
    specs = []
    for s in range(shards):
        regs = []
        for i, region in enumerate(cfg.regions):
            part = int(counts[i] // shards + (1 if s < counts[i] % shards else 0))
            if part > 0:
                regs.append(
                    {
                        "index": i,
                        "count": part,
                        "income_level": float(region.get("income_level", 1.0)),
                        "baseline_conf": float(region.get("baseline_conf", 0.0)),
                        "equity_level": float(region.get("equity_level", 0.3)),
                    }
                )
        specs.append(
            {
                "seed": int(cfg.seed) * 1009 + s,
                "regions": regs,
                "n_regions": R,
                "n_companies": C,
                "init_awareness": init_aw,
                "init_share": init_share,
                "essential": cfg.essential,
                "params": cfg.consumer_params,
            }
        )
    return specs, counts, shards


# ---------------------------------------------------------------------------
# Scenario events
# ---------------------------------------------------------------------------
OFFERING_STRENGTH = {"relief_supplies": 1.0, "free_services": 0.8, "donation": 0.5, "insights": 0.3}


class EventSchedule:
    def __init__(self, events: list[dict], cfg: SimConfig, n_agents: int, seed: int) -> None:
        self.R = len(cfg.regions)
        self.C = len(cfg.companies)
        self.essential = cfg.essential
        self.n_agents = n_agents
        self.rng = np.random.default_rng(seed + 7)
        self.region_index = {r["iso2"]: i for i, r in enumerate(cfg.regions)}
        self.company_index = {c["name"]: i for i, c in enumerate(cfg.companies)}
        startup = next((i for i, c in enumerate(cfg.companies) if c.get("is_startup")), 0)
        self.company_index["startup"] = startup
        self.events = [e for e in (events or []) if isinstance(e, dict) and e.get("type")]
        self.campaign_spend = np.zeros(self.C)

    def _regions(self, value) -> list[int]:
        if value in (None, "", "ALL", "GLOBAL"):
            return list(range(self.R))
        if isinstance(value, (list, tuple)):
            out = []
            for v in value:
                out.extend(self._regions(v))
            return sorted(set(out))
        idx = self.region_index.get(str(value).upper())
        return [idx] if idx is not None else []

    def _company(self, value) -> int | None:
        if value is None:
            return None
        return self.company_index.get(value, self.company_index.get(str(value)))

    @staticmethod
    def _window(t: int, start: int, duration: int) -> float | None:
        """Linear decay weight inside [start, start+duration), else None."""
        if t < start or t >= start + max(1, duration):
            return None
        return 1.0 - (t - start) / max(1, duration)

    def disaster_attention(self, t: int) -> np.ndarray:
        att = np.zeros(self.R)
        for e in self.events:
            if e["type"] != "disaster":
                continue
            w = self._window(t, int(e.get("start", 0)), int(e.get("duration", 30)))
            if w is None:
                continue
            for r in self._regions(e.get("region")):
                att[r] = max(att[r], float(e.get("severity", 0.5)) * (0.5 + 0.5 * w))
        return att

    def effects(self, t: int, companies: list[CompanyAgent]) -> dict:
        R, C = self.R, self.C
        news = np.zeros(R)
        fund = np.zeros(R)
        push = np.zeros(R)
        need = np.ones(R)
        media = np.zeros((C, R))
        rep = np.zeros((C, R))
        cost = np.zeros(C)
        cap = np.ones(C)
        boost = np.ones((C, R))
        attention = self.disaster_attention(t)

        for e in self.events:
            kind = e["type"]
            start = int(e.get("start", 0))
            regions = self._regions(e.get("region"))
            if kind == "news":
                w = self._window(t, start, int(e.get("duration", 10)))
                if w is not None:
                    news[regions] += float(e.get("value", 0.0)) * w
            elif kind == "market_shock":
                mag = float(e.get("magnitude", -0.05))
                if t in (start, start + 1):
                    fund[regions] += mag / 2.0
                w = self._window(t, start, 8)
                if w is not None:
                    news[regions] += mag * 3.0 * w
                if start <= t < start + 3:
                    push[regions] += mag * 20.0
            elif kind == "disaster":
                sev = float(np.clip(e.get("severity", 0.6), 0.0, 1.0))
                duration = int(e.get("duration", 30))
                w = self._window(t, start, duration)
                if w is not None:
                    news[regions] += -0.6 * sev * w
                    others = [i for i in range(R) if i not in regions]
                    if t < start + 5:
                        news[others] += -0.08 * sev
                    need[regions] *= (1 + 0.7 * sev * w) if self.essential else (1 - 0.45 * sev * w)
                    for ci, comp in enumerate(companies):
                        if any(r in comp.supply_regions for r in regions):
                            cap[ci] = min(cap[ci], 1 - 0.6 * sev * w)
                if t in (start, start + 1):
                    fund[regions] += -(0.015 + 0.05 * sev) / 2.0
                if start <= t < start + 3:
                    push[regions] += -1.5 * sev
            elif kind == "rate_shock":
                mag = float(e.get("magnitude", -0.25))
                w = self._window(t, start, int(e.get("duration", 15)))
                if w is not None:
                    news[regions] += mag * w
                if t == start:
                    fund[regions] += -0.02 * abs(mag) / 0.25
                if start <= t < start + 2:
                    push[regions] += -1.0 * abs(mag) / 0.25
            elif kind == "price_change":
                ci = self._company(e.get("company"))
                if ci is not None and t == start:
                    factor = 1.0 + float(e.get("pct", 0.0))
                    companies[ci].price *= factor
                    companies[ci].base_price *= factor
            elif kind == "marketing_push":
                ci = self._company(e.get("company", "startup"))
                w = self._window(t, start, int(e.get("duration", 30)))
                if ci is not None and w is not None:
                    boost[ci, regions] *= float(e.get("multiplier", 2.0))
            elif kind == "csr_campaign":
                ci = self._company(e.get("company", "startup"))
                duration = int(e.get("duration", 20))
                w = self._window(t, start, duration)
                if ci is None or w is None:
                    continue
                strength = OFFERING_STRENGTH.get(e.get("offering", "donation"), 0.5)
                genuine = 1.0 if e.get("style", "genuine") == "genuine" else 0.45
                budget = max(0.0, float(e.get("budget", 1.0)))  # in months of marketing spend
                bf = math.sqrt(budget)
                others = [i for i in range(R) if i not in regions]
                for r in regions:
                    att = max(0.3, attention[r])
                    media[ci, r] += 0.02 * att * strength * bf
                    rep[ci, r] += 0.012 * att * strength * genuine
                    media[ci, others] += 0.004 * att * strength * bf
                    rep[ci, others] += 0.002 * att * strength * genuine
                    if e.get("style") == "promotional" and self.rng.random() < 0.12 * att:
                        rep[ci, r] -= 0.05
                        rep[ci, others] -= 0.01
                spend = budget * 30 * self.n_agents * MARKETING_COST / max(1, duration)
                cost[ci] += spend
                self.campaign_spend[ci] += spend
        return {
            "news": news, "fund": fund, "opinion_push": push, "need": need, "media": media,
            "rep_delta": rep, "campaign_cost": cost, "capacity_factor": cap, "marketing_boost": boost,
        }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def _sum_parts(parts: list[dict]) -> dict:
    total = {}
    for key in parts[0]:
        if isinstance(parts[0][key], (int, float)):
            total[key] = sum(p[key] for p in parts)
        else:
            total[key] = np.sum([p[key] for p in parts], axis=0)
    return total


def _r(x, nd: int = 5):
    if isinstance(x, (list, tuple)):
        return [_r(v, nd) for v in x]
    if isinstance(x, dict):
        return {k: _r(v, nd) for k, v in x.items()}
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return None if (math.isnan(v) or math.isinf(v)) else round(v, nd)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.ndarray):
        return _r(x.tolist(), nd)
    return x


def run_simulation(cfg: SimConfig, progress: Progress | None = None,
                   cancelled: Callable[[], bool] | None = None) -> dict:
    started = time.perf_counter()
    R, C = len(cfg.regions), len(cfg.companies)
    specs, counts, n_shards = plan_shards(cfg)
    n_agents = int(counts.sum())
    region_w = counts / n_agents
    region_names = [r["name"] for r in cfg.regions]
    company_names = [c["name"] for c in cfg.companies]
    startup_idx = next((i for i, c in enumerate(cfg.companies) if c.get("is_startup")), None)

    companies = [CompanyAgent(spec, R, region_w, n_agents, cfg.seed + 17 * i) for i, spec in enumerate(cfg.companies)]
    markets: dict[int, TraderMarket] = {}
    for i, region in enumerate(cfg.regions):
        mk = region.get("market")
        if mk and mk.get("p0"):
            params = dict(mk.get("params") or {})
            params["n_traders"] = cfg.traders_per_market
            markets[i] = TraderMarket(params, float(mk["p0"]), cfg.seed + 101 * i)
    schedule = EventSchedule(cfg.events, cfg, n_agents, cfg.seed)

    backend = ProcessBackend(specs) if (cfg.use_processes and n_shards > 1) else LocalBackend(specs)
    baseline = np.array([float(r.get("baseline_conf", 0.0)) for r in cfg.regions])
    region_conf = baseline.copy()
    prev_conf = baseline.copy()
    global_conf = float((baseline * region_w).sum())
    last_ret = np.zeros(R)

    series: dict = {
        "t": [], "global_conf": [], "conformity": [], "global_market": [],
        "conf": {n: [] for n in region_names}, "panic": {n: [] for n in region_names},
        "euphoria": {n: [] for n in region_names},
        "market": {region_names[i]: {"price": [], "fundamental": [], "frac_c": [], "xbar": []} for i in markets},
        "share": {n: [] for n in company_names}, "awareness": {n: [] for n in company_names},
        "revenue": {n: [] for n in company_names}, "profit": {n: [] for n in company_names},
        "price": {n: [] for n in company_names}, "marketing": {n: [] for n in company_names},
        "stockout": {n: [] for n in company_names},
        "startup_awareness_by_region": {n: [] for n in region_names},
    }
    gm_level = 100.0
    mk_weights = np.array([region_w[i] for i in markets]) if markets else np.array([])
    mk_weights = mk_weights / mk_weights.sum() if mk_weights.size else mk_weights
    last_agg: dict | None = None

    try:
        if progress:
            progress(0.01, f"starting {n_agents:,} consumer agents in {n_shards} shard(s) ({backend.name})")
        backend.start()
        report_every = max(1, cfg.ticks // 50)
        for t in range(cfg.ticks):
            if cancelled is not None and cancelled():
                raise SimCancelled()
            fx = schedule.effects(t, companies)
            for ci, comp in enumerate(companies):
                comp.capacity_factor = float(fx["capacity_factor"][ci])
                comp.campaign_cost_tick = float(fx["campaign_cost"][ci])
                comp.marketing_boost = fx["marketing_boost"][ci]
                comp.reputation = np.clip(comp.reputation + fx["rep_delta"][ci], -1.0, 1.0)
            g = {
                "news_shock": fx["news"].astype(np.float32),
                "market_return": last_ret.astype(np.float32),
                "region_conf": region_conf.astype(np.float32),
                "baseline_conf": baseline.astype(np.float32),
                "global_conf": global_conf,
                "ad_reach": np.stack([c.ad_reach() for c in companies]).astype(np.float32),
                "media": fx["media"].astype(np.float32),
                "need": fx["need"].astype(np.float32),
                "price": np.array([c.price for c in companies], dtype=np.float32),
                "quality": np.array([c.quality for c in companies], dtype=np.float32),
                "reputation": np.stack([c.reputation for c in companies]).astype(np.float32),
                "available": np.stack([c.available for c in companies]),
                "stockout": np.stack([c.stockout for c in companies]).astype(np.float32),
            }
            agg = _sum_parts(backend.step(g))
            last_agg = agg
            cnt = np.maximum(agg["counts"], 1.0)
            region_conf = agg["conf_sum"] / cnt
            global_conf = float(agg["conf_sum"].sum() / cnt.sum())
            aff_mean = agg["aff_sum"] / cnt[None, :]
            for ci, comp in enumerate(companies):
                out = comp.settle(agg["buyers"][ci], agg["attempts"][ci], aff_mean[ci], t)
                name = company_names[ci]
                series["revenue"][name].append(out["revenue"])
                series["profit"][name].append(out["profit"])
                series["share"][name].append(float(agg["owners"][ci].sum() / n_agents))
                series["awareness"][name].append(float(agg["aw_sum"][ci].sum() / n_agents))
                series["price"][name].append(comp.price)
                series["marketing"][name].append(comp.marketing * float(comp.marketing_boost.mean()))
                series["stockout"][name].append(float(comp.stockout.mean()))

            new_ret = np.zeros(R)
            for i, market in markets.items():
                out = market.step(
                    fund_shock=float(fx["fund"][i]) + 0.03 * float(region_conf[i] - prev_conf[i]),
                    sentiment=float(region_conf[i] - baseline[i]),
                    opinion_push=float(fx["opinion_push"][i]),
                )
                new_ret[i] = out["ret"]
                m = series["market"][region_names[i]]
                m["price"].append(out["price"])
                m["fundamental"].append(out["fundamental"])
                m["frac_c"].append(out["frac_c"])
                m["xbar"].append(out["xbar"])
            if markets:
                gm_level *= math.exp(float((new_ret[list(markets)] * mk_weights).sum()))
            last_ret = new_ret
            prev_conf = region_conf.copy()

            series["t"].append(t)
            series["global_conf"].append(global_conf)
            series["global_market"].append(gm_level)
            series["conformity"].append(agg["conform"] / agg["choices"] if agg["choices"] else 0.0)
            for i, name in enumerate(region_names):
                series["conf"][name].append(float(region_conf[i]))
                series["panic"][name].append(float(agg["panic"][i] / cnt[i]))
                series["euphoria"][name].append(float(agg["euph"][i] / cnt[i]))
                if startup_idx is not None:
                    series["startup_awareness_by_region"][name].append(float(agg["aw_sum"][startup_idx][i] / cnt[i]))
            if progress and (t % report_every == 0 or t == cfg.ticks - 1):
                progress(0.03 + 0.9 * (t + 1) / cfg.ticks, f"tick {t + 1}/{cfg.ticks}")
    finally:
        backend.stop()

    # Final state and emergent pattern detection
    found: list[dict] = []
    for i in markets:
        m = series["market"][region_names[i]]
        found += pat.market_patterns(region_names[i], m["price"], m["fundamental"], m["frac_c"], m["xbar"])
    found += pat.consumer_patterns(region_names, series["panic"], series["conf"], series["conformity"])
    startup_name = company_names[startup_idx] if startup_idx is not None else None
    found += pat.company_patterns(company_names, series["share"], series["price"], startup_name)
    for p in found:
        p["start"] = int(p.get("start", 0))
        p["end"] = int(p.get("end", 0))

    cnt = np.maximum(last_agg["counts"], 1.0) if last_agg else np.ones(R)
    final_companies = []
    for ci, comp in enumerate(companies):
        share_by_region = (last_agg["owners"][ci] / cnt) if last_agg else np.zeros(R)
        final_companies.append({
            "name": comp.name,
            "is_startup": comp.is_startup,
            "share": series["share"][comp.name][-1] if series["share"][comp.name] else 0.0,
            "awareness": series["awareness"][comp.name][-1] if series["awareness"][comp.name] else 0.0,
            "revenue_total": comp.revenue_total,
            "profit_total": comp.profit_total,
            "units_total": comp.units_total,
            "campaign_spend": float(schedule.campaign_spend[ci]),
            "price": comp.price,
            "reputation_by_region": {region_names[i]: float(comp.reputation[i]) for i in range(R)},
            "share_by_region": {region_names[i]: float(share_by_region[i]) for i in range(R)},
        })
    final_markets = {}
    for i in markets:
        prices = series["market"][region_names[i]]["price"]
        if prices:
            arr = np.asarray(prices)
            peak = np.maximum.accumulate(arr)
            final_markets[region_names[i]] = {
                "start": float(arr[0]),
                "end": float(arr[-1]),
                "change_pct": float((arr[-1] / arr[0] - 1) * 100),
                "max_drawdown_pct": float((arr / peak - 1).min() * 100),
                "symbol": cfg.regions[i]["market"].get("symbol"),
            }
    result = {
        "meta": {
            "name": cfg.name,
            "agents": n_agents,
            "traders": cfg.traders_per_market * len(markets),
            "shards": n_shards,
            "backend": backend.name,
            "ticks": cfg.ticks,
            "seed": cfg.seed,
            "duration_s": round(time.perf_counter() - started, 2),
            "regions": [{"iso2": r["iso2"], "name": r["name"], "agents": int(counts[i]),
                         "baseline_conf": float(baseline[i]), "has_market": i in markets}
                        for i, r in enumerate(cfg.regions)],
            "companies": company_names,
            "startup": startup_name,
            "events": cfg.events,
        },
        "series": series,
        "final": {"companies": final_companies, "markets": final_markets},
        "patterns": found,
    }
    if progress:
        progress(0.97, f"done in {result['meta']['duration_s']}s, {len(found)} emergent patterns")
    return _r(result)


def compute_uplift(treated: dict, base: dict) -> dict:
    """Effect of the startup's own actions (treatment events) vs. the baseline run with the same seed."""
    def startup(res):
        return next((c for c in res["final"]["companies"] if c["is_startup"]), None)

    t, b = startup(treated), startup(base)
    if not t or not b:
        return {}
    out = {
        "share_pp": round((t["share"] - b["share"]) * 100, 3),
        "awareness_pp": round((t["awareness"] - b["awareness"]) * 100, 3),
        "revenue_pct": round((t["revenue_total"] / b["revenue_total"] - 1) * 100, 2) if b["revenue_total"] else None,
        "profit_delta": round(t["profit_total"] - b["profit_total"], 2),
        "units_pct": round((t["units_total"] / b["units_total"] - 1) * 100, 2) if b["units_total"] else None,
        "campaign_spend": round(t["campaign_spend"], 2),
        "by_region": {},
    }
    if t["campaign_spend"] > 0:
        out["roi"] = round((t["profit_total"] - b["profit_total"]) / t["campaign_spend"], 3)
    aw_t = treated["series"]["startup_awareness_by_region"]
    aw_b = base["series"]["startup_awareness_by_region"]
    for region in t["share_by_region"]:
        out["by_region"][region] = {
            "share_pp": round((t["share_by_region"][region] - b["share_by_region"].get(region, 0)) * 100, 3),
            "reputation_delta": round(t["reputation_by_region"][region] - b["reputation_by_region"].get(region, 0), 4),
            "awareness_pp": round(((aw_t.get(region) or [0])[-1] - (aw_b.get(region) or [0])[-1]) * 100, 3),
        }
    return out
