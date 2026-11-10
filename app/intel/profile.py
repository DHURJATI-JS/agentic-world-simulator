"""First-run onboarding: the AI interviews the founder, then the structured profile is confirmed."""
from __future__ import annotations

import json
import logging

from sqlalchemy import delete, select

import config
from app.ai.base import AIRequest
from app.ai.prompts import ONBOARD_SCHEMA
from app.data.geo import geocode, resolve_country
from app.data.http import make_client
from app.db import SessionLocal
from app.models import OnboardingMessage, StartupProfile, utcnow
from app.runtime import runtime

log = logging.getLogger("worldsim.onboarding")

GREETING = (
    "Welcome! Before I start analysing the world for you, I'd like to understand your startup. "
    "What is it called, and what does it do?"
)

ONBOARDING_SYSTEM = """You are the onboarding assistant of Agentic World Simulator, a market simulator that tailors
global insights to one startup. Interview the founder. Ask ONE short, friendly question at a time and
never repeat a question that was already answered. You need to learn:
1. the startup's name and what it does
2. its products and/or services (offerings) and sector
3. whether it sells PHYSICAL products
4. if it sells physical products: can it ship domestically? internationally? to which countries?
5. whether it offers services, and whether it is digital-only (software/online)
6. HQ city and country
7. target markets (countries)
8. stage and team size
9. any business insurance it already has
Keep everything learned so far in "profile". Capability fields take "yes", "no" or "unknown".
Use ISO-3166 alpha-2 codes (IN, US, JP, ...) for hq_country, ships_to and target_markets; use
"WORLDWIDE" in ships_to if it ships everywhere. Put the topics still unknown in "missing".
Set "done" to true only once items 1-7 are known; then "reply" must briefly summarise the profile
and ask the founder to confirm or correct it."""

EMPTY_PROFILE = {
    "name": "", "description": "", "sector": "", "business_model": "", "offerings": [],
    "sells_physical_products": "unknown", "offers_services": "unknown", "digital_only": "unknown",
    "can_ship_domestic": "unknown", "can_ship_international": "unknown", "ships_to": [],
    "target_markets": [], "hq_city": "", "hq_country": "", "stage": "", "team_size": "", "insured_for": [],
}


async def get_messages() -> list[dict]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(OnboardingMessage).order_by(OnboardingMessage.id))).scalars().all()
        if not rows:
            greeting = OnboardingMessage(role="assistant", content=GREETING, meta={"profile": EMPTY_PROFILE})
            session.add(greeting)
            await session.commit()
            rows = [greeting]
        return [r.to_dict() for r in rows]


_JUNK_MARKERS = ("”}", '"}', "”,\n", "\n}", "} {")


def clean_reply(text: str) -> str:
    """Small models sometimes keep writing after they 'closed' the JSON string; cut that tail off."""
    text = str(text or "").strip()
    for marker in _JUNK_MARKERS:
        idx = text.find(marker)
        if idx > 20:
            text = text[:idx]
    return text.strip().strip("{").strip().rstrip('"”').strip()


def _latest_partial(messages: list[dict]) -> dict:
    for m in reversed(messages):
        if m["role"] == "assistant" and (m.get("meta") or {}).get("profile"):
            return m["meta"]["profile"]
    return dict(EMPTY_PROFILE)


async def onboarding_turn(user_message: str) -> dict:
    text = (user_message or "").strip()[: config.CHAT_MAX_MESSAGE_CHARS]
    if not text:
        raise ValueError("empty message")
    messages = await get_messages()
    async with SessionLocal() as session:
        session.add(OnboardingMessage(role="user", content=text))
        await session.commit()
    messages.append({"role": "user", "content": text})

    partial = _latest_partial(messages)
    transcript = "\n".join(
        f"{'ASSISTANT' if m['role'] == 'assistant' else 'FOUNDER'}: {m['content']}" for m in messages[-14:]
    )
    prompt = (
        f"Profile learned so far (JSON):\n{json.dumps(partial, ensure_ascii=False)}\n\n"
        f"Conversation so far:\n{transcript}\n\n"
        "Update the profile with anything new from the founder's last message, then write your next reply."
    )
    data, result = await runtime.ai.generate_json(
        AIRequest(
            system=ONBOARDING_SYSTEM,
            prompt=prompt,
            purpose="onboarding",
            schema=ONBOARD_SCHEMA,
            schema_name="onboarding_turn",
            depth="fast",
            temperature=0.2,
        ),
        interactive=True,
        use_cache=False,
        validator=lambda d: isinstance(d, dict) and isinstance(d.get("reply"), str),
    )
    profile = {**EMPTY_PROFILE, **(data.get("profile") or {})}
    reply = clean_reply(data.get("reply")) or "Could you tell me a bit more?"
    done = bool(data.get("done"))
    missing = [str(m) for m in data.get("missing") or []][:10]
    async with SessionLocal() as session:
        session.add(
            OnboardingMessage(
                role="assistant",
                content=reply,
                meta={"profile": profile, "done": done, "missing": missing, "provider": result.provider_label},
            )
        )
        await session.commit()
    return {
        "reply": reply,
        "done": done,
        "missing": missing,
        "profile": normalize_profile(profile),
        "provider": result.provider_label,
        "messages": await get_messages(),
    }


def _tri(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"yes", "true", "1", "y"}


def _countries(values) -> list[str]:
    out = []
    for v in values or []:
        text = str(v).strip()
        if not text:
            continue
        if text.upper() in {"WORLDWIDE", "GLOBAL", "ALL"}:
            code = "WORLDWIDE"
        else:
            code = resolve_country(text)
        if code and code not in out:
            out.append(code)
    return out


def normalize_profile(raw: dict) -> dict:
    sells = _tri(raw.get("sells_physical_products"))
    digital = _tri(raw.get("digital_only"))
    services = _tri(raw.get("offers_services")) or (digital and not sells)
    hq_country = resolve_country(str(raw.get("hq_country") or "")) or ""
    return {
        "name": str(raw.get("name") or "").strip()[:200],
        "description": str(raw.get("description") or "").strip()[:2000],
        "sector": str(raw.get("sector") or "").strip()[:200],
        "business_model": str(raw.get("business_model") or "").strip()[:200],
        "offerings": [str(o).strip()[:120] for o in raw.get("offerings") or [] if str(o).strip()][:20],
        "sells_physical_products": sells,
        "offers_services": services,
        "digital_only": digital and not sells,
        "can_ship_domestic": sells and _tri(raw.get("can_ship_domestic")),
        "can_ship_international": sells and _tri(raw.get("can_ship_international")),
        "ships_to": _countries(raw.get("ships_to")) if sells else [],
        "target_markets": [c for c in _countries(raw.get("target_markets")) if c != "WORLDWIDE"],
        "hq_city": str(raw.get("hq_city") or "").strip()[:200],
        "hq_country": hq_country,
        "stage": str(raw.get("stage") or "").strip()[:100],
        "team_size": str(raw.get("team_size") or "").strip()[:100],
        "insured_for": [str(i).strip()[:120] for i in raw.get("insured_for") or [] if str(i).strip()][:20],
    }


async def save_profile(data: dict, *, complete: bool = True) -> dict:
    """Validate and store the startup profile (geocoding the HQ for location-risk monitoring)."""
    clean = normalize_profile(data)
    if not clean["name"]:
        raise ValueError("The startup needs a name.")
    if not clean["hq_country"]:
        raise ValueError("Please give the HQ country.")
    location = None
    if clean["hq_city"]:
        async with make_client() as client:
            location = await geocode(client, clean["hq_city"], clean["hq_country"])
    if location is None:
        from app.data.geo import countries as country_table  # centroid fallback

        c = country_table().get(clean["hq_country"])
        location = {"lat": c.lat, "lon": c.lon} if c else {"lat": None, "lon": None}
    async with SessionLocal() as session:
        row = (await session.execute(select(StartupProfile).order_by(StartupProfile.id).limit(1))).scalar_one_or_none()
        if row is None:
            row = StartupProfile()
            session.add(row)
        for key, value in clean.items():
            setattr(row, key, value)
        row.hq_lat = location.get("lat")
        row.hq_lon = location.get("lon")
        row.onboarding_complete = complete
        row.updated_at = utcnow()
        await session.commit()
        return row.to_dict()


async def reset_onboarding() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(OnboardingMessage))
        row = (await session.execute(select(StartupProfile).limit(1))).scalar_one_or_none()
        if row:
            row.onboarding_complete = False
        await session.commit()
