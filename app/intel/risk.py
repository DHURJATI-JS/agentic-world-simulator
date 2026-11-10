"""
Location risk for the startup HQ: seismic anomaly detection (USGS counts vs. a Poisson
baseline), long-term seismic hazard, 7-day weather extremes and hazards near the HQ.
The AI turns these into warnings plus insurance and preparedness recommendations.
"""
from __future__ import annotations

import logging

from sqlalchemy import update

from app.ai.base import AIRequest
from app.ai.prompts import ANALYST_SYSTEM, RISK_SCHEMA, profile_block
from app.data.geo import country_name, geocode, haversine_km
from app.data.hazards import hq_alert_radius, seismic_scan, weather_scan
from app.data.http import make_client
from app.db import SessionLocal
from app.intel.common import get_state, load_profile, recent_insight, save_insight, set_state
from app.models import GeoEvent, StartupProfile, utcnow
from app.runtime import runtime

log = logging.getLogger("worldsim.risk")

LEVEL_TO_SEVERITY = {"critical": "critical", "high": "warning", "moderate": "watch", "low": "info"}


async def ensure_hq(profile: dict) -> tuple[float, float] | None:
    if profile.get("hq_lat") is not None and profile.get("hq_lon") is not None:
        return float(profile["hq_lat"]), float(profile["hq_lon"])
    async with make_client() as client:
        loc = await geocode(client, profile.get("hq_city") or "", profile.get("hq_country"))
    if not loc or loc.get("lat") is None:
        return None
    async with SessionLocal() as session:
        await session.execute(
            update(StartupProfile).where(StartupProfile.id == profile["id"]).values(hq_lat=loc["lat"], hq_lon=loc["lon"])
        )
        await session.commit()
    return float(loc["lat"]), float(loc["lon"])


async def _ai_risk(profile: dict, facts: str, purpose: str):
    prompt = (
        f"{profile_block(profile)}\n"
        f"Existing insurance: {', '.join(profile.get('insured_for') or []) or 'none mentioned'}\n\n"
        f"LOCATION RISK FACTS:\n{facts}\n\n"
        "Warn the founder clearly but without alarmism: overall risk level, what is abnormal (if anything), "
        "immediate actions, which insurance policies to take or review (e.g. earthquake, business interruption, "
        "property/contents, flood, key-person, cargo for stock in transit - only those that fit this startup and "
        "that it does not already have) and preparedness steps."
    )
    return await runtime.ai.generate_json(
        AIRequest(system=ANALYST_SYSTEM, prompt=prompt, purpose=purpose, schema=RISK_SCHEMA,
                  schema_name="risk_alert", depth="deep"),
        validator=lambda d: isinstance(d, dict) and d.get("headline"),
    )


async def location_scan(force: bool = False, progress=None) -> dict:
    profile = await load_profile()
    if not profile or not profile.get("onboarding_complete"):
        return {"skipped": "no startup profile yet"}
    hq = await ensure_hq(profile)
    if not hq:
        return {"skipped": "HQ location unknown"}
    lat, lon = hq
    if progress:
        await progress(0.1, "USGS seismic history around HQ")
    seismic = await seismic_scan(lat, lon)
    if progress:
        await progress(0.4, "7-day weather forecast at HQ")
    try:
        weather = await weather_scan(lat, lon)
    except Exception as exc:  # noqa: BLE001
        weather = {"extremes": [], "error": str(exc)}
    state = {
        "lat": lat, "lon": lon, "city": profile.get("hq_city"), "country": profile.get("hq_country"),
        "seismic": seismic, "weather_extremes": weather.get("extremes", []), "checked_at": utcnow().isoformat(),
    }
    await set_state("risk_profile", state)
    runtime.hub.publish("risk", state)

    triggers = []
    if seismic["abnormal"]:
        triggers.append(
            f"ABNORMAL seismic activity: {seismic['quakes_last_7d']} earthquakes M2.5+ within {seismic['radius_km']} km "
            f"in the last 7 days vs a normal {seismic['baseline_weekly_rate']}/week (Poisson p={seismic['p_value']})"
        )
    for ex in weather.get("extremes", []):
        triggers.append(f"Forecast {ex['type']} on {ex['date']}: {ex['value']}")
    facts = (
        f"HQ: {profile.get('hq_city')}, {country_name(profile.get('hq_country'))} ({lat:.3f}, {lon:.3f})\n"
        f"Long-term seismic hazard: {seismic['long_term_hazard']} ({seismic['m5_plus_last_50y']} quakes M5+ within "
        f"{seismic['radius_km']} km in 50 years)\n"
        f"Last 7 days: {seismic['quakes_last_7d']} quakes M2.5+ (normal rate {seismic['baseline_weekly_rate']}/week)\n"
        + ("\n".join(f"- {t}" for t in triggers) if triggers else "- no current anomaly")
    )
    if triggers:
        key = "risk:anomaly"
        if not force and await recent_insight(key, 12 * 3600):
            return {"checked": True, "triggers": triggers, "skipped": "alert already issued in the last 12h"}
    elif seismic["long_term_hazard"] in ("moderate", "high"):
        key = "risk:baseline"
        if not force and await recent_insight(key, 7 * 86400):
            return {"checked": True, "triggers": []}
    else:
        return {"checked": True, "triggers": [], "hazard": seismic["long_term_hazard"]}

    if progress:
        await progress(0.6, "AI risk assessment")
    data, result = await _ai_risk(profile, facts, "location_risk")
    data["facts"] = state
    data["triggers"] = triggers
    severity = LEVEL_TO_SEVERITY.get(data.get("risk_level"), "watch")
    insurance_recs = [
        {"title": f"Insurance: {i.get('type')}", "detail": i.get("why", ""), "priority": i.get("priority", "medium"),
         "requires": "none"}
        for i in data.get("insurance") or [] if isinstance(i, dict)
    ]
    insight = await save_insight(
        kind="risk_alert",
        title=data.get("headline") or "Location risk update",
        summary=data.get("summary") or "",
        content=data,
        recommendations=insurance_recs,
        region=profile.get("hq_country"),
        severity=severity,
        result=result,
        dedupe_key=key,
        profile=profile,
        notify_level="critical" if severity == "critical" else ("warning" if triggers else "info"),
    )
    return {"insight_id": insight["id"], "triggers": triggers}


async def event_alert(event_id: int, progress=None) -> dict:
    profile = await load_profile()
    if not profile or not profile.get("onboarding_complete"):
        return {"skipped": "no profile"}
    hq = await ensure_hq(profile)
    async with SessionLocal() as session:
        event = await session.get(GeoEvent, event_id)
        if event is None:
            return {"skipped": "event not found"}
        ev = event.to_dict()
    distance = ev.get("near_hq_km")
    if distance is None and hq:
        distance = haversine_km(hq[0], hq[1], ev["lat"], ev["lon"])
    radius = hq_alert_radius(ev)
    facts = (
        f"HQ: {profile.get('hq_city')}, {country_name(profile.get('hq_country'))}\n"
        f"EVENT NEAR HQ: {ev['kind']} - {ev['title']}; magnitude {ev['magnitude']}; depth {ev['depth_km']} km; "
        f"alert {ev['alert_level'] or 'n/a'}; tsunami flag {ev['tsunami']}; "
        f"{f'{distance:.0f} km from HQ' if distance is not None else 'distance to HQ unknown'} "
        f"(alert radius for this event {radius:.0f} km); time {ev['occurred_at']}"
    )
    data, result = await _ai_risk(profile, facts, "hq_event_alert")
    data["event"] = ev
    data["distance_km"] = distance
    critical = (distance is not None and distance <= radius / 2) or (ev.get("magnitude") or 0) >= 6.5 or ev.get("alert_level") == "red"
    insurance_recs = [
        {"title": f"Insurance: {i.get('type')}", "detail": i.get("why", ""), "priority": i.get("priority", "medium"),
         "requires": "none"}
        for i in data.get("insurance") or [] if isinstance(i, dict)
    ]
    insight = await save_insight(
        kind="risk_alert",
        title=data.get("headline") or f"{ev['kind'].title()} near your HQ",
        summary=data.get("summary") or "",
        content=data,
        recommendations=insurance_recs,
        region=profile.get("hq_country"),
        severity="critical" if critical else "warning",
        result=result,
        dedupe_key=f"riskev:{event_id}",
        related_event_id=event_id,
        profile=profile,
        notify_level="critical" if critical else "warning",
    )
    return {"insight_id": insight["id"], "distance_km": distance}


async def risk_profile_state() -> dict:
    return await get_state("risk_profile")


__all__ = ["event_alert", "location_scan", "risk_profile_state"]
