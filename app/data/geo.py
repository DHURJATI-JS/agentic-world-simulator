"""
Geography helpers: country shapes (Natural Earth, shared with the globe), point-in-polygon
lookup for hazards, a gazetteer to tag headlines by country, distances and geocoding.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

log = logging.getLogger("worldsim.geo")

GEOJSON_PATH = Path(__file__).resolve().parents[2] / "static" / "vendor" / "countries.geojson"
_ISO_FIX = {"France": "FR", "Norway": "NO", "Somaliland": "SO", "Northern Cyprus": "CY", "Kosovo": "XK"}


@dataclass
class Country:
    iso2: str
    iso3: str
    name: str
    lat: float
    lon: float
    continent: str = ""
    subregion: str = ""
    pop_est: float = 0.0
    rings: list[np.ndarray] = field(default_factory=list, repr=False)
    bboxes: list[tuple[float, float, float, float]] = field(default_factory=list, repr=False)


def _ring_centroid(ring: np.ndarray) -> tuple[float, float, float]:
    x, y = ring[:, 0], ring[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    cross = x * y2 - x2 * y
    area = cross.sum() / 2.0
    if abs(area) < 1e-9:
        return float(x.mean()), float(y.mean()), 0.0
    cx = ((x + x2) * cross).sum() / (6 * area)
    cy = ((y + y2) * cross).sum() / (6 * area)
    return float(cx), float(cy), abs(float(area))


@lru_cache(maxsize=1)
def countries() -> dict[str, Country]:
    try:
        data = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.exception("could not load %s", GEOJSON_PATH)
        return {}
    out: dict[str, Country] = {}
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        admin = props.get("ADMIN") or props.get("NAME") or ""
        iso2 = props.get("ISO_A2")
        if not iso2 or iso2 == "-99":
            iso2 = _ISO_FIX.get(admin)
        if not iso2:
            continue
        geom = feature.get("geometry") or {}
        polys = geom.get("coordinates") or []
        if geom.get("type") == "Polygon":
            polys = [polys]
        rings, boxes = [], []
        best = (0.0, 0.0, -1.0)
        for poly in polys:
            if not poly:
                continue
            ring = np.asarray(poly[0], dtype=np.float64)
            if ring.ndim != 2 or ring.shape[0] < 3:
                continue
            rings.append(ring)
            boxes.append((ring[:, 0].min(), ring[:, 1].min(), ring[:, 0].max(), ring[:, 1].max()))
            cx, cy, area = _ring_centroid(ring)
            if area > best[2]:
                best = (cx, cy, area)
        if not rings:
            continue
        iso3 = props.get("ISO_A3") if props.get("ISO_A3") not in (None, "-99") else props.get("ADM0_A3", "")
        if iso2 in out:  # merge duplicate (e.g. Northern Cyprus into Cyprus)
            out[iso2].rings.extend(rings)
            out[iso2].bboxes.extend(boxes)
            continue
        out[iso2] = Country(
            iso2=iso2,
            iso3=iso3 or "",
            name=props.get("NAME") or admin,
            lat=best[1],
            lon=best[0],
            continent=props.get("CONTINENT", ""),
            subregion=props.get("SUBREGION", ""),
            pop_est=float(props.get("POP_EST") or 0),
            rings=rings,
            bboxes=boxes,
        )
    return out


def country_name(iso2: str | None) -> str:
    if not iso2:
        return ""
    c = countries().get(iso2)
    return c.name if c else iso2


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _in_ring(lon: float, lat: float, ring: np.ndarray) -> bool:
    x, y = ring[:, 0], ring[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    crosses = (y > lat) != (y2 > lat)
    with np.errstate(divide="ignore", invalid="ignore"):
        x_int = (x2 - x) * (lat - y) / (y2 - y) + x
    return bool(np.count_nonzero(crosses & (lon < x_int)) % 2)


def country_at(lat: float, lon: float, max_offshore_km: float = 450.0) -> str | None:
    """ISO2 of the country containing the point, else the nearest coast within max_offshore_km."""
    table = countries()
    for iso2, c in table.items():
        for ring, (x0, y0, x1, y1) in zip(c.rings, c.bboxes):
            if x0 <= lon <= x1 and y0 <= lat <= y1 and _in_ring(lon, lat, ring):
                return iso2
    best_iso, best_km = None, max_offshore_km
    lat_r = math.radians(lat)
    for iso2, c in table.items():
        for ring, (x0, y0, x1, y1) in zip(c.rings, c.bboxes):
            if lon < x0 - 8 or lon > x1 + 8 or lat < y0 - 8 or lat > y1 + 8:
                continue
            dlat = np.radians(ring[:, 1]) - lat_r
            dlon = np.radians(ring[:, 0] - lon)
            a = np.sin(dlat / 2) ** 2 + np.cos(lat_r) * np.cos(np.radians(ring[:, 1])) * np.sin(dlon / 2) ** 2
            km = float(2 * 6371.0 * np.arcsin(np.sqrt(np.clip(a.min(), 0, 1))))
            if km < best_km:
                best_iso, best_km = iso2, km
    return best_iso


# ---------------------------------------------------------------------------
# Gazetteer for tagging headlines with countries
# ---------------------------------------------------------------------------
ALIASES: dict[str, list[str]] = {
    "US": ["United States", "U.S.", "U.S", "USA", "US", "America", "Americans", "Washington", "White House",
           "Pentagon", "Wall Street", "Federal Reserve", "Fed", "Nasdaq", "Dow Jones", "S&P 500", "New York",
           "California", "Texas", "Florida", "Chicago", "Los Angeles", "Silicon Valley", "US Congress"],
    "CA": ["Canada", "Canadian", "Ottawa", "Toronto", "Vancouver", "Montreal", "Quebec"],
    "MX": ["Mexico", "Mexican", "Mexico City"],
    "BR": ["Brazil", "Brazilian", "Brasilia", "Sao Paulo", "São Paulo", "Rio de Janeiro", "Lula"],
    "AR": ["Argentina", "Argentine", "Argentinian", "Buenos Aires", "Milei"],
    "CL": ["Chile", "Chilean", "Santiago"],
    "CO": ["Colombia", "Colombian", "Bogota", "Bogotá"],
    "PE": ["Peru", "Peruvian", "Lima"],
    "VE": ["Venezuela", "Venezuelan", "Caracas"],
    "GB": ["United Kingdom", "U.K.", "UK", "Britain", "British", "England", "London", "Scotland",
           "Wales", "Downing Street", "Bank of England", "FTSE"],
    "IE": ["Ireland", "Irish", "Dublin"],
    "FR": ["France", "French", "Paris", "Macron", "Elysee", "Élysée", "CAC 40"],
    "DE": ["Germany", "German", "Berlin", "Frankfurt", "Munich", "Bundesbank", "DAX"],
    "IT": ["Italy", "Italian", "Rome", "Milan"],
    "ES": ["Spain", "Spanish", "Madrid", "Barcelona"],
    "PT": ["Portugal", "Portuguese", "Lisbon"],
    "NL": ["Netherlands", "Dutch", "Amsterdam", "The Hague"],
    "BE": ["Belgium", "Belgian", "Brussels"],
    "CH": ["Switzerland", "Swiss", "Zurich", "Geneva"],
    "AT": ["Austria", "Austrian", "Vienna"],
    "SE": ["Sweden", "Swedish", "Stockholm"],
    "NO": ["Norway", "Norwegian", "Oslo"],
    "DK": ["Denmark", "Danish", "Copenhagen", "Greenland"],
    "FI": ["Finland", "Finnish", "Helsinki"],
    "PL": ["Poland", "Polish", "Warsaw"],
    "CZ": ["Czech Republic", "Czechia", "Czech", "Prague"],
    "HU": ["Hungary", "Hungarian", "Budapest", "Orban", "Orbán"],
    "RO": ["Romania", "Romanian", "Bucharest"],
    "GR": ["Greece", "Greek", "Athens"],
    "TR": ["Turkey", "Türkiye", "Turkiye", "Turkish", "Ankara", "Istanbul", "Erdogan", "Erdoğan"],
    "UA": ["Ukraine", "Ukrainian", "Kyiv", "Kiev", "Kharkiv", "Odesa", "Zelensky", "Zelenskyy"],
    "RU": ["Russia", "Russian", "Moscow", "Kremlin", "Putin", "St Petersburg"],
    "IL": ["Israel", "Israeli", "Jerusalem", "Tel Aviv", "Netanyahu"],
    "PS": ["Gaza", "Palestinian", "Palestine", "West Bank", "Hamas"],
    "LB": ["Lebanon", "Lebanese", "Beirut", "Hezbollah"],
    "SY": ["Syria", "Syrian", "Damascus"],
    "IR": ["Iran", "Iranian", "Tehran"],
    "IQ": ["Iraq", "Iraqi", "Baghdad"],
    "SA": ["Saudi Arabia", "Saudi", "Riyadh", "Aramco"],
    "AE": ["United Arab Emirates", "UAE", "Emirati", "Dubai", "Abu Dhabi"],
    "QA": ["Qatar", "Qatari", "Doha"],
    "YE": ["Yemen", "Yemeni", "Houthi", "Houthis"],
    "EG": ["Egypt", "Egyptian", "Cairo"],
    "NG": ["Nigeria", "Nigerian", "Lagos", "Abuja"],
    "ZA": ["South Africa", "South African", "Johannesburg", "Cape Town", "Pretoria"],
    "KE": ["Kenya", "Kenyan", "Nairobi"],
    "ET": ["Ethiopia", "Ethiopian", "Addis Ababa"],
    "SD": ["Sudan", "Sudanese", "Khartoum"],
    "MA": ["Morocco", "Moroccan", "Rabat", "Casablanca"],
    "DZ": ["Algeria", "Algerian", "Algiers"],
    "GH": ["Ghana", "Ghanaian", "Accra"],
    "CD": ["Democratic Republic of Congo", "DR Congo", "DRC", "Kinshasa"],
    "IN": ["India", "Indian", "Indians", "New Delhi", "Delhi", "Mumbai", "Bengaluru", "Bangalore", "Chennai",
           "Kolkata", "Hyderabad", "Pune", "Ahmedabad", "Sensex", "Nifty", "RBI", "Modi", "Lok Sabha",
           "Rajya Sabha", "Kerala", "Maharashtra", "Karnataka", "Tamil Nadu", "Gujarat", "Uttar Pradesh",
           "Bihar", "Punjab", "Kashmir", "Rupee"],
    "PK": ["Pakistan", "Pakistani", "Islamabad", "Karachi", "Lahore"],
    "BD": ["Bangladesh", "Bangladeshi", "Dhaka"],
    "LK": ["Sri Lanka", "Sri Lankan", "Colombo"],
    "NP": ["Nepal", "Nepalese", "Nepali", "Kathmandu"],
    "AF": ["Afghanistan", "Afghan", "Kabul", "Taliban"],
    "CN": ["China", "Chinese", "Beijing", "Shanghai", "Shenzhen", "Guangzhou", "Hong Kong", "Hang Seng",
           "Xi Jinping", "Yuan", "Renminbi"],
    "TW": ["Taiwan", "Taiwanese", "Taipei", "TSMC"],
    "JP": ["Japan", "Japanese", "Tokyo", "Osaka", "Kyoto", "Hokkaido", "Okinawa", "Nikkei", "Bank of Japan",
           "BOJ", "Yen", "Fukushima", "Noto"],
    "KR": ["South Korea", "South Korean", "Korea", "Korean", "Seoul", "Busan", "Kospi", "Samsung"],
    "KP": ["North Korea", "North Korean", "Pyongyang", "Kim Jong Un"],
    "VN": ["Vietnam", "Vietnamese", "Hanoi", "Ho Chi Minh City"],
    "TH": ["Thailand", "Thai", "Bangkok"],
    "MY": ["Malaysia", "Malaysian", "Kuala Lumpur"],
    "ID": ["Indonesia", "Indonesian", "Jakarta", "Bali", "Sumatra"],
    "PH": ["Philippines", "Philippine", "Filipino", "Manila"],
    "MM": ["Myanmar", "Burmese", "Yangon", "Naypyidaw"],
    "AU": ["Australia", "Australian", "Canberra", "Sydney", "Melbourne", "Brisbane", "Perth"],
    "NZ": ["New Zealand", "Wellington", "Auckland"],
}


@lru_cache(maxsize=1)
def _alias_regex() -> tuple[re.Pattern, dict[str, str]]:
    """Case-sensitive: aliases are proper nouns ('China' the country, not 'china' the porcelain)."""
    mapping: dict[str, str] = {}
    for iso2, names in ALIASES.items():
        for alias in names:
            mapping[alias] = iso2
    body = "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
    return re.compile(rf"(?<![\w.])({body})(?![\w])"), mapping


_FALSE_FRIENDS = re.compile(
    r"\b(Indian Ocean|Latin America\w*|South America\w*|Central America\w*|North America\w*|"
    r"American football|New York Times|Washington Post|Native Americans?)\b"
)


def tag_countries(text: str) -> list[str]:
    """Return ISO2 codes mentioned in the text, in order of first mention."""
    if not text:
        return []
    cleaned = _FALSE_FRIENDS.sub(" ", text)
    pattern, mapping = _alias_regex()
    seen: list[str] = []
    for m in pattern.finditer(cleaned):
        iso = mapping.get(m.group(1))
        if iso and iso not in seen:
            seen.append(iso)
    return seen[:6]


def resolve_country(value: str | None) -> str | None:
    """Turn 'India', 'IND', 'in', 'Japanese' ... into an ISO2 code."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    table = countries()
    upper = text.upper()
    if len(upper) == 2 and (upper in table or upper in ALIASES):
        return upper
    for iso2, c in table.items():
        if upper == c.iso3 or text.lower() == c.name.lower():
            return iso2
    tags = tag_countries(text) or tag_countries(text.title())
    if tags:
        return tags[0]
    lowered = text.lower()
    for iso2, c in table.items():
        if lowered in c.name.lower() or c.name.lower() in lowered:
            return iso2
    return None


async def geocode(client, city: str, country: str | None = None) -> dict | None:
    """Geocode a city with the Open-Meteo geocoding API (no key needed)."""
    if not city:
        return None
    try:
        resp = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 10, "language": "en", "format": "json"},
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("geocoding failed for %s: %s", city, exc)
        return None
    iso = resolve_country(country) if country else None
    if iso:
        matching = [r for r in results if (r.get("country_code") or "").upper() == iso]
        results = matching or results
    if not results:
        return None
    best = max(results, key=lambda r: r.get("population") or 0)
    return {
        "lat": best.get("latitude"),
        "lon": best.get("longitude"),
        "country": (best.get("country_code") or "").upper() or iso,
        "name": best.get("name"),
        "admin1": best.get("admin1"),
    }
