"""
Natural hazards: USGS earthquakes + GDACS multi-hazard alerts, severity scoring, and
location risk scans for the startup HQ (seismic anomaly vs. baseline, 7-day weather extremes).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

import config
from app.data.geo import country_at, country_name, haversine_km, resolve_country
from app.data.http import make_client
from app.db import SessionLocal
from app.models import GeoEvent, utcnow

log = logging.getLogger("worldsim.hazards")

USGS_FEEDS = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_week.geojson",
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson",
)
USGS_COUNT = "https://earthquake.usgs.gov/fdsnws/event/1/count"
GDACS_EVENTS = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS4APP"
GDACS_KINDS = {"EQ": "earthquake", "TC": "cyclone", "FL": "flood", "VO": "volcano", "DR": "drought", "WF": "wildfire"}
ALERT_WEIGHT = {"red": 0.45, "orange": 0.28, "yellow": 0.12, "green": 0.0}


def quake_severity(mag: float | None, depth: float | None, alert: str | None, tsunami: bool) -> float:
    base = max(0.0, min(1.0, ((mag or 0) - 4.5) / 3.5))
    depth_factor = 1.0 if (depth or 0) < 70 else (0.75 if (depth or 0) < 300 else 0.5)
    score = base * depth_factor + ALERT_WEIGHT.get(alert or "", 0.0) + (0.1 if tsunami else 0.0)
    return round(max(0.0, min(1.0, score)), 3)


def gdacs_severity(kind: str, alert: str | None, severity_value: float | None) -> float:
    base = {"red": 0.85, "orange": 0.6, "green": 0.2}.get(alert or "", 0.25)
    if kind == "cyclone" and severity_value:
        base = max(base, min(1.0, (severity_value - 60) / 200))  # km/h wind
    return round(base, 3)


def _usgs_event(feature: dict) -> dict | None:
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    if len(coords) < 2 or props.get("type") not in (None, "earthquake"):
        return None
    lon, lat = float(coords[0]), float(coords[1])
    depth = float(coords[2]) if len(coords) > 2 and coords[2] is not None else None
    mag = props.get("mag")
    alert = props.get("alert")
    tsunami = bool(props.get("tsunami"))
    place = props.get("place") or ""
    iso = country_at(lat, lon)
    if not iso and "," in place:
        iso = resolve_country(place.rsplit(",", 1)[-1])
    return {
        "external_id": f"usgs:{feature.get('id')}",
        "source": "USGS",
        "kind": "earthquake",
        "title": props.get("title") or f"M {mag} - {place}",
        "place": place,
        "country": iso,
        "country_name": country_name(iso) if iso else "",
        "lat": lat,
        "lon": lon,
        "depth_km": depth,
        "magnitude": float(mag) if mag is not None else None,
        "alert_level": alert,
        "tsunami": tsunami,
        "severity": quake_severity(mag, depth, alert, tsunami),
        "occurred_at": datetime.fromtimestamp((props.get("time") or 0) / 1000, tz=timezone.utc),
        "url": props.get("url") or "",
        "raw": {"sig": props.get("sig"), "felt": props.get("felt"), "mmi": props.get("mmi")},
    }


def _parse_gdacs_time(value: str | None) -> datetime:
    if not value:
        return utcnow()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt[: len(fmt)]).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return utcnow()


def _gdacs_event(feature: dict) -> dict | None:
    props = feature.get("properties") or {}
    code = props.get("eventtype")
    kind = GDACS_KINDS.get(code)
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    if not kind or len(coords) < 2:
        return None
    lon, lat = float(coords[0]), float(coords[1])
    alert = (props.get("alertlevel") or "").lower() or None
    sev = props.get("severitydata") or {}
    sev_value = sev.get("severity")
    try:
        sev_value = float(sev_value) if sev_value is not None else None
    except (TypeError, ValueError):
        sev_value = None
    iso = resolve_country(props.get("country") or "") or country_at(lat, lon)
    report = (props.get("url") or {}).get("report") if isinstance(props.get("url"), dict) else ""
    return {
        "external_id": f"gdacs:{code}:{props.get('eventid')}",
        "source": "GDACS",
        "kind": kind,
        "title": props.get("name") or props.get("eventname") or kind.title(),
        "place": props.get("country") or "",
        "country": iso,
        "country_name": country_name(iso) if iso else (props.get("country") or ""),
        "lat": lat,
        "lon": lon,
        "depth_km": None,
        "magnitude": sev_value if kind == "earthquake" else None,
        "alert_level": alert,
        "tsunami": False,
        "severity": gdacs_severity(kind, alert, sev_value if kind == "cyclone" else None),
        "occurred_at": _parse_gdacs_time(props.get("fromdate")),
        "url": report or "",
        "raw": {"severity": sev, "episode": props.get("episodeid"), "to": props.get("todate")},
    }


def hq_alert_radius(event: dict) -> float:
    if event["kind"] == "earthquake":
        mag = event.get("magnitude") or 0
        for threshold, radius in sorted(config.HQ_ALERT_RADII_KM.items(), reverse=True):
            if mag >= threshold:
                return radius
        return 0.0
    return {"red": 600, "orange": 400, "green": 150}.get(event.get("alert_level") or "", 150)


async def refresh_hazards(hq: tuple[float, float] | None = None) -> dict:
    events: dict[str, dict] = {}
    errors = []
    async with make_client() as client:
        for url in USGS_FEEDS:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                for feature in resp.json().get("features", []):
                    ev = _usgs_event(feature)
                    if ev:
                        events[ev["external_id"]] = ev
            except Exception as exc:  # noqa: BLE001
                errors.append(f"USGS: {exc}")
        try:
            resp = await client.get(GDACS_EVENTS)
            resp.raise_for_status()
            usgs_quakes = [e for e in events.values() if e["kind"] == "earthquake"]
            for feature in resp.json().get("features", []):
                ev = _gdacs_event(feature)
                if not ev:
                    continue
                if ev["kind"] == "earthquake" and any(
                    haversine_km(ev["lat"], ev["lon"], q["lat"], q["lon"]) < 150
                    and abs((ev["occurred_at"] - q["occurred_at"]).total_seconds()) < 6 * 3600
                    for q in usgs_quakes
                ):
                    continue  # same quake already reported by USGS
                events[ev["external_id"]] = ev
        except Exception as exc:  # noqa: BLE001
            errors.append(f"GDACS: {exc}")

    if hq:
        for ev in events.values():
            ev["near_hq_km"] = round(haversine_km(hq[0], hq[1], ev["lat"], ev["lon"]), 1)

    async with SessionLocal() as session:
        existing = {
            row.external_id: row
            for row in (
                await session.execute(select(GeoEvent).where(GeoEvent.external_id.in_(list(events))))
            ).scalars()
        }
        new_ids: list[str] = []
        for ev in events.values():
            values = {**ev, "updated_at": utcnow()}
            stmt = insert(GeoEvent).values(**values)
            update_cols = {k: stmt.excluded[k] for k in values if k not in ("external_id",)}
            await session.execute(stmt.on_conflict_do_update(index_elements=["external_id"], set_=update_cols))
            if ev["external_id"] not in existing:
                new_ids.append(ev["external_id"])
        await session.commit()
        rows = {
            row.external_id: row
            for row in (
                await session.execute(select(GeoEvent).where(GeoEvent.external_id.in_(list(events))))
            ).scalars()
        }

    significant, near_hq = [], []
    for ext_id, row in rows.items():
        ev = events[ext_id]
        is_new = ext_id in new_ids
        escalated = ext_id in existing and (existing[ext_id].alert_level or "") != (ev.get("alert_level") or "")
        if (row.severity >= config.HAZARD_SIGNIFICANT_SEVERITY or (row.alert_level in ("orange", "red"))) and (
            is_new or escalated or not row.analyzed
        ):
            if row.occurred_at >= utcnow() - timedelta(days=4):
                significant.append(row.id)
        if hq and row.near_hq_km is not None and (is_new or escalated):
            if row.near_hq_km <= hq_alert_radius(ev):
                near_hq.append(row.id)
    return {
        "events": len(events),
        "new": len(new_ids),
        "significant_ids": significant,
        "near_hq_ids": near_hq,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Location risk scans (startup HQ)
# ---------------------------------------------------------------------------
def poisson_tail(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


async def _usgs_count(client, lat: float, lon: float, radius: float, minmag: float,
                      start: datetime, end: datetime | None = None) -> int:
    params = {
        "format": "geojson",
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "maxradiuskm": int(radius),
        "minmagnitude": minmag,
        "starttime": start.strftime("%Y-%m-%d"),
    }
    if end:
        params["endtime"] = end.strftime("%Y-%m-%d")
    resp = await client.get(USGS_COUNT, params=params)
    resp.raise_for_status()
    data = resp.json()
    return int(data.get("count", 0)) if isinstance(data, dict) else int(data)


async def seismic_scan(lat: float, lon: float, radius_km: float | None = None) -> dict:
    radius = radius_km or config.SEISMIC_SCAN_RADIUS_KM
    now = utcnow()
    async with make_client(timeout=40) as client:
        recent = await _usgs_count(client, lat, lon, radius, 2.5, now - timedelta(days=7))
        baseline = await _usgs_count(client, lat, lon, radius, 2.5, now - timedelta(days=372), now - timedelta(days=7))
        historic = await _usgs_count(client, lat, lon, radius, 5.0, now - timedelta(days=365 * 50))
    weekly_rate = baseline / 52.0
    p_value = poisson_tail(recent, weekly_rate)
    abnormal = recent >= 3 and p_value < 0.01
    if historic >= 50:
        hazard = "high"
    elif historic >= 10:
        hazard = "moderate"
    elif historic >= 1:
        hazard = "low"
    else:
        hazard = "very low"
    return {
        "radius_km": radius,
        "quakes_last_7d": recent,
        "baseline_weekly_rate": round(weekly_rate, 3),
        "p_value": round(p_value, 5),
        "abnormal": abnormal,
        "ratio_vs_baseline": round(recent / weekly_rate, 2) if weekly_rate > 0 else None,
        "m5_plus_last_50y": historic,
        "long_term_hazard": hazard,
        "checked_at": now.isoformat(),
    }


async def weather_scan(lat: float, lon: float) -> dict:
    async with make_client() as client:
        resp = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "daily": "precipitation_sum,wind_gusts_10m_max,temperature_2m_max,temperature_2m_min",
                "forecast_days": 7,
                "timezone": "auto",
            },
        )
        resp.raise_for_status()
        daily = resp.json().get("daily") or {}
    extremes = []
    days = daily.get("time") or []
    for i, day in enumerate(days):
        rain = (daily.get("precipitation_sum") or [None] * len(days))[i]
        gust = (daily.get("wind_gusts_10m_max") or [None] * len(days))[i]
        tmax = (daily.get("temperature_2m_max") or [None] * len(days))[i]
        tmin = (daily.get("temperature_2m_min") or [None] * len(days))[i]
        if rain is not None and rain >= 100:
            extremes.append({"date": day, "type": "extreme rainfall / flood risk", "value": f"{rain:.0f} mm"})
        elif rain is not None and rain >= 50:
            extremes.append({"date": day, "type": "heavy rainfall", "value": f"{rain:.0f} mm"})
        if gust is not None and gust >= 90:
            extremes.append({"date": day, "type": "damaging wind gusts", "value": f"{gust:.0f} km/h"})
        if tmax is not None and tmax >= 43:
            extremes.append({"date": day, "type": "extreme heat", "value": f"{tmax:.0f} °C"})
        if tmin is not None and tmin <= -20:
            extremes.append({"date": day, "type": "extreme cold", "value": f"{tmin:.0f} °C"})
    return {"days": len(days), "extremes": extremes, "daily": daily}
