"""World Bank indicators (GDP per capita, population, inflation) - used to calibrate agent populations."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.data.geo import countries
from app.data.http import make_client
from app.db import SessionLocal
from app.models import CountryStat, utcnow

log = logging.getLogger("worldsim.worldbank")

INDICATORS = {
    "gdp_per_capita": "NY.GDP.PCAP.CD",
    "population": "SP.POP.TOTL",
    "inflation": "FP.CPI.TOTL.ZG",
}
BASE = "https://api.worldbank.org/v2/country/{codes}/indicator/{indicator}"


async def refresh_country_stats() -> dict:
    table = countries()
    iso3_to_iso2 = {c.iso3: iso2 for iso2, c in table.items() if c.iso3 and len(c.iso3) == 3}
    values: dict[str, dict] = {iso2: {"iso2": iso2, "iso3": table[iso2].iso3, "name": table[iso2].name} for iso2 in table}
    errors = []
    async with make_client(timeout=60) as client:
        for field, indicator in INDICATORS.items():
            # One request for all economies: a single unknown code would make a batched request fail.
            try:
                resp = await client.get(
                    BASE.format(codes="all", indicator=indicator),
                    params={"format": "json", "mrnev": 1, "per_page": 1000},
                )
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, list) and payload and isinstance(payload[0], dict) and payload[0].get("message"):
                    errors.append(f"{indicator}: {payload[0]['message']}")
                    continue
                rows = payload[1] if isinstance(payload, list) and len(payload) > 1 and payload[1] else []
                for row in rows:
                    iso2 = iso3_to_iso2.get(row.get("countryiso3code"))
                    if iso2 and row.get("value") is not None:
                        values[iso2][field] = float(row["value"])
                        values[iso2]["year"] = row.get("date")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{indicator}: {exc}")

    # Natural Earth population as a fallback so every country has a weight
    for iso2, item in values.items():
        if item.get("population") is None and table[iso2].pop_est:
            item["population"] = table[iso2].pop_est

    async with SessionLocal() as session:
        for item in values.values():
            row = {
                "iso2": item["iso2"],
                "iso3": item.get("iso3") or "",
                "name": item.get("name") or "",
                "gdp_per_capita": item.get("gdp_per_capita"),
                "population": item.get("population"),
                "inflation": item.get("inflation"),
                "year": item.get("year"),
                "updated_at": utcnow(),
            }
            stmt = insert(CountryStat).values(**row)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["iso2"], set_={k: stmt.excluded[k] for k in row if k != "iso2"}
                )
            )
        await session.commit()
    with_gdp = sum(1 for v in values.values() if v.get("gdp_per_capita"))
    return {"countries": len(values), "with_gdp": with_gdp, "errors": errors[:5]}


async def load_stats() -> dict[str, CountryStat]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(CountryStat))).scalars().all()
    return {r.iso2: r for r in rows}
