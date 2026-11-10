"""AI interpretation of a finished simulation run (stored as a timestamped insight)."""
from __future__ import annotations

import json

from sqlalchemy import update

from app.ai.base import AIRequest
from app.ai.prompts import ANALYST_SYSTEM, SIMREPORT_SCHEMA, clamp, profile_block
from app.db import SessionLocal
from app.intel.common import load_profile, save_insight
from app.models import SimulationRun
from app.runtime import runtime

SEV_RANK = {"high": 0, "medium": 1, "low": 2}


async def report(run_id: int, progress=None) -> dict:
    async with SessionLocal() as session:
        run = await session.get(SimulationRun, run_id)
        if run is None or run.status != "done":
            return {"skipped": "run not finished"}
        results, patterns, uplift = run.results or {}, run.patterns or [], run.uplift or {}
        name, scenario = run.name, run.scenario or {}
    meta = results.get("meta", {})
    final = results.get("final", {})
    profile = await load_profile()
    regions = "\n".join(
        f"- {r['name']}: {r['agents']:,} agents, starting confidence {r['baseline_conf']:+.2f}, stock market model: {r['has_market']}"
        for r in meta.get("regions", [])
    )
    markets = "\n".join(
        f"- {k}: {v['change_pct']:+.1f}% over the run, max drawdown {v['max_drawdown_pct']:.1f}%"
        for k, v in (final.get("markets") or {}).items()
    ) or "- none"
    companies = "\n".join(
        f"- {c['name']}{' (the startup)' if c['is_startup'] else ''}: customer share {c['share']*100:.1f}%, "
        f"awareness {c['awareness']*100:.0f}%, revenue {c['revenue_total']:.0f}, profit {c['profit_total']:.0f}, "
        f"final price {c['price']:.2f}"
        for c in final.get("companies", [])
    )
    top_patterns = sorted(patterns, key=lambda p: SEV_RANK.get(p.get("severity"), 3))[:12]
    pattern_text = "\n".join(f"- [{p['severity']}] {p['label']} (ticks {p['start']}-{p['end']}): {p['description']}"
                             for p in top_patterns) or "- none detected"
    prompt = (
        f"{profile_block(profile)}\n\n"
        f"SIMULATION '{name}': {meta.get('agents', 0):,} consumer agents + {meta.get('traders', 0):,} trader agents, "
        f"{meta.get('ticks')} daily ticks, {meta.get('shards')} shard(s).\n"
        f"Scenario events: {json.dumps(scenario.get('events', []))[:1500]}\n\n"
        f"REGIONS:\n{regions}\n\nMARKETS:\n{markets}\n\nCOMPANIES:\n{companies}\n\n"
        f"EFFECT VS BASELINE (same seed, without the startup's own actions or without the shocks): "
        f"{json.dumps({k: v for k, v in uplift.items() if k != 'by_region'})}\n\n"
        f"EMERGENT PATTERNS DETECTED (not scripted):\n{pattern_text}\n\n"
        "Interpret the run for the founder: what emerged and why (which agent interactions produced it), the key "
        "risks and opportunities, and 3-5 recommendations. Note that this is a model, not a forecast certainty."
    )
    data, result = await runtime.ai.generate_json(
        AIRequest(system=ANALYST_SYSTEM, prompt=prompt, purpose="simulation_report", schema=SIMREPORT_SCHEMA,
                  schema_name="simulation_report", depth="deep"),
        validator=lambda d: isinstance(d, dict) and d.get("headline"),
    )
    data["confidence"] = clamp(data.get("confidence"), 0, 1, 0.5)
    data["run_id"] = run_id
    insight = await save_insight(
        kind="simulation_report",
        title=data.get("headline") or f"Simulation report: {name}",
        summary=data.get("summary") or "",
        content=data,
        recommendations=data.get("recommendations"),
        region=None,
        severity="info",
        result=result,
        related_run_id=run_id,
        profile=profile,
        notify_level="success",
    )
    async with SessionLocal() as session:
        await session.execute(update(SimulationRun).where(SimulationRun.id == run_id).values(report_insight_id=insight["id"]))
        await session.commit()
    return {"insight_id": insight["id"]}
