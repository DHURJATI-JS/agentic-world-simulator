"""System prompts and JSON schemas shared by the AI pipelines.

Schemas use only features every tier supports (llama.cpp grammar, Ollama and Claude
structured outputs): object/array/string/number/integer/boolean, enum, required,
additionalProperties=false.
"""
from __future__ import annotations

from typing import Any

ANALYST_SYSTEM = (
    "You are the analyst inside 'Agentic World Simulator', an agent-based market simulator that "
    "helps a startup test its strategy against real-world conditions before acting. Ground every "
    "statement in the facts provided (headlines from reliable news outlets, market data, hazard "
    "feeds, simulation output). Never invent events or numbers; if the data is thin, say so. Be "
    "concrete and brief. Forecasts and probabilities are model estimates, not financial advice. "
    'In your JSON, "headline" is a short title for your own analysis (never a news headline or a '
    "remark about missing data)."
)

STR: dict = {"type": "string"}
NUM: dict = {"type": "number"}
INT: dict = {"type": "integer"}
BOOL: dict = {"type": "boolean"}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


def enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


def obj(props: dict[str, dict], required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": False,
    }


REQUIRES = ("none", "physical_products", "shipping_domestic", "shipping_international", "services")

RECOMMENDATION = obj(
    {
        "title": STR,
        "detail": STR,
        "priority": enum("high", "medium", "low"),
        "requires": enum(*REQUIRES),
    }
)

CATEGORIES = (
    "economy", "markets", "business", "technology", "politics", "conflict", "disaster",
    "climate", "energy", "health", "society", "sports", "other",
)

ENRICH_SCHEMA = obj(
    {
        "items": arr(
            obj(
                {
                    "id": INT,
                    "countries": arr(STR),
                    "category": enum(*CATEGORIES),
                    "sentiment": NUM,
                    "importance": NUM,
                    "market_relevance": NUM,
                }
            )
        )
    }
)

CLUSTER_SCHEMA = obj(
    {
        "summary": STR,
        "themes": arr(obj({"title": STR, "summary": STR, "regions": arr(STR), "sentiment": NUM})),
    }
)

BRIEF_SCHEMA = obj(
    {
        "headline": STR,
        "summary": STR,
        "global_mood": NUM,
        "themes": arr(
            obj(
                {
                    "title": STR,
                    "summary": STR,
                    "regions": arr(STR),
                    "sentiment": NUM,
                    "market_impact": STR,
                }
            )
        ),
        "market_implications": arr(STR),
        "watchlist": arr(STR),
        "recommendations": arr(RECOMMENDATION),
    }
)

SCENARIO = obj(
    {
        "name": enum("bear", "base", "bull"),
        "probability": NUM,
        "description": STR,
        "expected_move_pct": NUM,
    }
)

OUTLOOK_SCHEMA = obj(
    {
        "headline": STR,
        "situation": STR,
        "drivers": arr(STR),
        "consumer_reaction": STR,
        "investor_reaction": STR,
        "scenarios": arr(SCENARIO),
        "horizon_days": INT,
        "signals_to_watch": arr(STR),
        "impact_on_startup": STR,
        "recommendations": arr(RECOMMENDATION),
        "confidence": NUM,
    }
)

REGION_SCHEMA = obj(
    {
        "headline": STR,
        "situation": STR,
        "key_stories": arr(STR),
        "drivers": arr(STR),
        "consumer_reaction": STR,
        "investor_reaction": STR,
        "scenarios": arr(SCENARIO),
        "horizon_days": INT,
        "risks": arr(STR),
        "opportunities": arr(STR),
        "impact_on_startup": STR,
        "recommendations": arr(RECOMMENDATION),
        "confidence": NUM,
    }
)

NEED_CATEGORIES = (
    "water", "food", "shelter", "medical", "power", "communication", "hygiene", "clothing",
    "transport", "services", "financial", "other",
)

GEO_SCHEMA = obj(
    {
        "headline": STR,
        "summary": STR,
        "humanitarian_needs": arr(
            obj(
                {
                    "item": STR,
                    "category": enum(*NEED_CATEGORIES),
                    "urgency": enum("critical", "high", "medium"),
                    "why": STR,
                }
            )
        ),
        "market_impact": obj(
            {
                "local_market": STR,
                "currency": STR,
                "sectors_up": arr(STR),
                "sectors_down": arr(STR),
                "expected_duration": STR,
            }
        ),
        "supply_chain_risks": arr(STR),
        "opportunity": STR,
        "ethics_note": STR,
        "recommendations": arr(RECOMMENDATION),
    }
)

RISK_SCHEMA = obj(
    {
        "headline": STR,
        "risk_level": enum("low", "moderate", "high", "critical"),
        "summary": STR,
        "immediate_actions": arr(STR),
        "insurance": arr(obj({"type": STR, "why": STR, "priority": enum("high", "medium", "low")})),
        "preparedness": arr(STR),
    }
)

TRI = enum("yes", "no", "unknown")

ONBOARD_SCHEMA = obj(
    {
        "reply": STR,
        "done": BOOL,
        "missing": arr(STR),
        "profile": obj(
            {
                "name": STR,
                "description": STR,
                "sector": STR,
                "business_model": STR,
                "offerings": arr(STR),
                "sells_physical_products": TRI,
                "offers_services": TRI,
                "digital_only": TRI,
                "can_ship_domestic": TRI,
                "can_ship_international": TRI,
                "ships_to": arr(STR),
                "target_markets": arr(STR),
                "hq_city": STR,
                "hq_country": STR,
                "stage": STR,
                "team_size": STR,
                "insured_for": arr(STR),
            }
        ),
    }
)

PERSONA_SCHEMA = obj(
    {
        "mood": NUM,
        "feeling": STR,
        "actions": arr(STR),
        "spending_intent": enum("cut", "hold", "increase"),
        "investing_intent": enum("sell", "hold", "buy", "none"),
        "would_try_startup": enum("yes", "maybe", "no"),
        "reason": STR,
        "importance": NUM,
    }
)

SIMREPORT_SCHEMA = obj(
    {
        "headline": STR,
        "summary": STR,
        "emergent_findings": arr(obj({"pattern": STR, "explanation": STR})),
        "risks": arr(STR),
        "opportunities": arr(STR),
        "recommendations": arr(RECOMMENDATION),
        "confidence": NUM,
    }
)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------
def yn(value: Any) -> str:
    return "yes" if value else "no"


def profile_block(profile: dict | None) -> str:
    """Compact startup description + the capability rule every recommendation must respect."""
    if not profile or not profile.get("name"):
        return (
            "STARTUP: not described yet. Give general market insight only; do not suggest shipping "
            "products or offering services."
        )
    ships_to = ", ".join(profile.get("ships_to") or []) or ("worldwide" if profile.get("can_ship_international") else "-")
    lines = [
        f"STARTUP: {profile.get('name')} - {profile.get('description', '')}".strip(),
        f"Sector: {profile.get('sector') or '-'}; model: {profile.get('business_model') or '-'}; "
        f"stage: {profile.get('stage') or '-'}; team: {profile.get('team_size') or '-'}",
        f"Offerings: {', '.join(profile.get('offerings') or []) or '-'}",
        f"HQ: {profile.get('hq_city') or '-'}, {profile.get('hq_country') or '-'}; "
        f"target markets: {', '.join(profile.get('target_markets') or []) or '-'}",
        "Capabilities: sells physical products=" + yn(profile.get("sells_physical_products"))
        + "; ships domestically=" + yn(profile.get("can_ship_domestic"))
        + "; ships internationally=" + yn(profile.get("can_ship_international"))
        + f" (to: {ships_to}); offers services=" + yn(profile.get("offers_services"))
        + "; digital only=" + yn(profile.get("digital_only")),
        "RULE: only recommend actions this startup can actually do. If it does not sell physical "
        "products it cannot ship goods - give market insights or service-based actions instead. "
        "Tag every recommendation with what it requires.",
    ]
    return "\n".join(lines)


def clip_lines(lines: list[str], max_chars: int) -> list[str]:
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            break
        out.append(line)
        used += len(line) + 1
    return out


def clamp(value: Any, low: float, high: float, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))
