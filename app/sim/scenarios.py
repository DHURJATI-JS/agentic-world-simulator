"""
Scenario presets. Events tagged "treatment": true are things the startup itself does;
when a run is compared against a baseline, the baseline drops those events (or, if a
scenario has no treatment events, drops every event) with the same random seed.
"""
from __future__ import annotations

EVENT_TYPES = {
    "news": "News sentiment shock (value -1..1, duration ticks)",
    "market_shock": "Stock market shock (magnitude, e.g. -0.07 = -7% to fundamentals)",
    "disaster": "Natural disaster (severity 0..1, duration ticks)",
    "rate_shock": "Interest-rate / macro shock (magnitude, e.g. -0.25)",
    "price_change": "Company price change (pct, e.g. -0.2)",
    "marketing_push": "Marketing push (multiplier, duration)",
    "csr_campaign": "Relief / CSR campaign (offering, budget in months of marketing, style)",
}

OFFERING_BY_FIT = {"ship_supplies": "relief_supplies", "offer_services": "free_services", "insights_only": "donation"}


def relief_fit(profile: dict | None, region: str | None) -> str:
    """What kind of help this startup can realistically offer in a region."""
    if not profile:
        return "insights_only"
    hq = (profile.get("hq_country") or "").upper()
    ships_to = {s.upper() for s in (profile.get("ships_to") or [])}
    if profile.get("sells_physical_products"):
        domestic = region and region.upper() == hq and profile.get("can_ship_domestic")
        international = profile.get("can_ship_international") and (
            not ships_to or "WORLDWIDE" in ships_to or (region or "").upper() in ships_to
        )
        if domestic or international:
            return "ship_supplies"
    if profile.get("offers_services") or profile.get("digital_only"):
        return "offer_services"
    return "insights_only"


def presets(profile: dict | None, default_disaster_region: str = "JP") -> list[dict]:
    hq = ((profile or {}).get("hq_country") or "IN").upper()
    targets = [t.upper() for t in ((profile or {}).get("target_markets") or [])] or [hq]
    fit = relief_fit(profile, default_disaster_region)
    offering = OFFERING_BY_FIT[fit]
    fit_text = {
        "ship_supplies": "ships relief supplies",
        "offer_services": "offers its services free to affected people/businesses",
        "insights_only": "cannot ship products, so it donates / partners with relief NGOs",
    }[fit]
    return [
        {
            "id": "baseline",
            "name": "Business as usual",
            "description": "No shocks: watch organic adoption, competition and market dynamics emerge.",
            "events": [],
            "compare_baseline": False,
        },
        {
            "id": "market_downturn",
            "name": f"Stock market downturn in {hq}",
            "description": f"A -7% shock hits {hq}'s market fundamentals; see whether panic spreads to "
                           "consumers and how the startup's demand reacts.",
            "events": [
                {"type": "market_shock", "region": hq, "magnitude": -0.07, "start": 20},
                {"type": "news", "region": hq, "value": -0.25, "start": 20, "duration": 12},
            ],
            "compare_baseline": True,
        },
        {
            "id": "disaster_relief",
            "name": f"Earthquake in {default_disaster_region} + relief campaign",
            "description": f"Severe earthquake (severity 0.8). The startup {fit_text}. "
                           "Compared with not helping, to estimate attention and reputation effects.",
            "events": [
                {"type": "disaster", "region": default_disaster_region, "severity": 0.8, "start": 15,
                 "duration": 35, "kind": "earthquake"},
                {"type": "csr_campaign", "company": "startup", "region": default_disaster_region,
                 "offering": offering, "budget": 1.0, "start": 18, "duration": 20, "style": "genuine",
                 "treatment": True},
            ],
            "compare_baseline": True,
            "fit": fit,
        },
        {
            "id": "disaster_promotional",
            "name": f"Earthquake in {default_disaster_region} + promotional campaign",
            "description": "Same disaster, but the help is heavily branded/promotional - tests backlash risk.",
            "events": [
                {"type": "disaster", "region": default_disaster_region, "severity": 0.8, "start": 15,
                 "duration": 35, "kind": "earthquake"},
                {"type": "csr_campaign", "company": "startup", "region": default_disaster_region,
                 "offering": offering, "budget": 1.0, "start": 18, "duration": 20, "style": "promotional",
                 "treatment": True},
            ],
            "compare_baseline": True,
            "fit": fit,
        },
        {
            "id": "price_war",
            "name": "Competitor price cut",
            "description": "The Challenger cuts prices 20%. Adaptive competitors learn; does a price war emerge?",
            "events": [{"type": "price_change", "company": "Challenger", "pct": -0.20, "start": 30}],
            "compare_baseline": True,
        },
        {
            "id": "global_risk_off",
            "name": "Global risk-off (US crash contagion)",
            "description": "US market shock plus a macro/rate shock everywhere: does fear propagate across regions?",
            "events": [
                {"type": "market_shock", "region": "US", "magnitude": -0.08, "start": 20},
                {"type": "rate_shock", "region": "ALL", "magnitude": -0.25, "start": 22, "duration": 15},
            ],
            "compare_baseline": True,
        },
        {
            "id": "launch_push",
            "name": "Launch campaign in target markets",
            "description": f"Triple marketing in {', '.join(targets)} for 30 ticks - does adoption tip?",
            "events": [
                {"type": "marketing_push", "company": "startup", "region": targets, "multiplier": 3.0,
                 "start": 10, "duration": 30, "treatment": True},
            ],
            "compare_baseline": True,
        },
    ]


def baseline_events(events: list[dict]) -> list[dict]:
    treated = [e for e in events if e.get("treatment")]
    if treated:
        return [e for e in events if not e.get("treatment")]
    return []
