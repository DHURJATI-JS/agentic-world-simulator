"""
LLM persona agents - "how are people reacting?"

Representative consumers/investors in key regions read today's headlines and market
moves and react in character. Each persona has a memory stream in pgvector; relevant
memories are retrieved by recency x importance x similarity (Generative-Agents style).
Their moods also seed the simulation's starting consumer confidence for that region.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta

import numpy as np
from sqlalchemy import desc, select

from app.ai.base import AIRequest
from app.ai.prompts import PERSONA_SCHEMA, clamp
from app.data.geo import country_name
from app.data.sources import PRIMARY_INDEX
from app.db import SessionLocal
from app.intel.common import get_state, load_profile, market_line, news_lines, retrieve_news, save_insight, set_state
from app.intel.outlook import _quote
from app.models import PersonaMemory, utcnow
from app.runtime import runtime

log = logging.getLogger("worldsim.personas")

PERSONAS: dict[str, list[tuple[str, str]]] = {
    "IN": [
        ("in-mumbai-saver", "Priya, 34, salaried IT professional in Mumbai who invests a monthly SIP in index funds"),
        ("in-bengaluru-retailer", "Ravi, 47, runs a small electronics shop in Bengaluru and watches supplier prices closely"),
    ],
    "US": [
        ("us-ohio-family", "Jessica, 41, nurse and mother of two in Columbus, Ohio, saving in a 401(k)"),
        ("us-sf-engineer", "Marcus, 29, software engineer in San Francisco holding tech stocks"),
    ],
    "JP": [
        ("jp-osaka-retiree", "Hiroshi, 68, retiree in Osaka living on a pension and savings"),
        ("jp-tokyo-worker", "Yuki, 31, office worker in Tokyo who rents a small apartment"),
    ],
    "GB": [("gb-manchester-teacher", "Sarah, 38, secondary-school teacher in Manchester with a mortgage")],
    "DE": [("de-munich-engineer", "Lukas, 45, automotive engineer in Munich worried about the car industry")],
    "CN": [("cn-shanghai-manager", "Li Wei, 36, marketing manager in Shanghai who owns an apartment")],
    "BR": [("br-saopaulo-seller", "Ana, 33, runs a small online shop in São Paulo")],
    "ID": [("id-jakarta-ojol", "Budi, 29, ride-hailing driver in Jakarta supporting his parents"),
           ("id-surabaya-warung", "Siti, 44, runs a family warung (small shop) in Surabaya")],
    "KR": [("kr-seoul-worker", "Ji-ho, 35, semiconductor engineer in Seoul with a stock portfolio")],
    "TW": [("tw-taipei-designer", "Mei-ling, 32, graphic designer in Taipei")],
    "AU": [("au-sydney-nurse", "Olivia, 40, nurse in Sydney paying off a mortgage")],
    "CA": [("ca-toronto-teacher", "Daniel, 37, teacher in Toronto")],
    "MX": [("mx-cdmx-vendor", "Carlos, 45, market vendor in Mexico City")],
    "FR": [("fr-lyon-baker", "Claire, 50, runs a bakery in Lyon")],
    "SG": [("sg-banker", "Wei Jie, 34, banker in Singapore")],
    "AE": [("ae-dubai-expat", "Arjun, 31, expat accountant in Dubai")],
}

PERSONA_SYSTEM = (
    "You simulate a realistic person for market research. Stay in character, think about your own money, "
    "job, family and plans, and react proportionately - most news barely changes most people's lives. "
    "Answer only as JSON."
)


def select_personas(profile: dict | None, max_n: int = 6) -> list[tuple[str, str, str]]:
    regions: list[str] = []
    if profile:
        regions.append((profile.get("hq_country") or "").upper())
        regions += [t.upper() for t in profile.get("target_markets") or []]
    regions += ["IN", "US", "JP", "GB", "DE", "CN", "BR"]
    ordered = [r for i, r in enumerate(regions) if r and r not in regions[:i]]
    chosen: list[tuple[str, str, str]] = []
    for depth in (0, 1):
        for iso in ordered:
            options = PERSONAS.get(iso) or [(f"{iso.lower()}-resident", f"a middle-income adult living in {country_name(iso)}")]
            if depth < len(options) and len(chosen) < max_n:
                key, desc = options[depth]
                chosen.append((key, desc, iso))
    return chosen


async def recall(persona_key: str, query_vec: np.ndarray, model: str, k: int = 4) -> list[str]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(PersonaMemory)
                .where(PersonaMemory.persona_key == persona_key, PersonaMemory.embed_model == model)
                .order_by(desc(PersonaMemory.created_at))
                .limit(40)
            )
        ).scalars().all()
    if not rows:
        return []
    now = utcnow()
    scored = []
    for row in rows:
        hours = max(0.0, (now - row.created_at).total_seconds() / 3600)
        recency = math.exp(-hours / 72.0)
        emb = np.asarray(row.embedding, dtype=float) if row.embedding is not None else None
        similarity = float(np.dot(emb, query_vec) / ((np.linalg.norm(emb) * np.linalg.norm(query_vec)) or 1.0)) if emb is not None else 0.0
        scored.append((0.4 * recency + 0.3 * (row.importance or 0.5) + 0.3 * similarity, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [f"({r.created_at:%d %b}) {r.content}" for _, r in scored[:k]]


async def pulse(force: bool = False, progress=None) -> dict:
    state = await get_state("persona_pulse")
    if not force and state.get("at"):
        from datetime import datetime

        last = datetime.fromisoformat(state["at"])
        if utcnow() - last < timedelta(hours=20):
            return {"skipped": "persona pulse already ran in the last 20h"}
    profile = await load_profile()
    personas = select_personas(profile)
    startup_text = "no startup described"
    if profile and profile.get("name"):
        offer = ", ".join(profile.get("offerings") or []) or profile.get("description", "")
        startup_text = f"'{profile['name']}' offers: {offer[:200]}"
    reactions = []
    for i, (key, desc, iso) in enumerate(personas, start=1):
        try:
            articles = await retrieve_news(f"{country_name(iso)} economy prices jobs cost of living markets",
                                           countries=[iso], hours=72, limit=6)
            quote = await _quote(PRIMARY_INDEX.get(iso))
            query_vec, model = await runtime.embedder.embed_one(desc + " " + " ".join(a["title"] for a in articles))
            memories = await recall(key, query_vec, model)
            prompt = (
                f"You are {desc}.\n\nNews you came across recently:\n{news_lines(articles, 1800)}\n\n"
                f"Local stock market: {market_line(quote)}\n\n"
                f"Your recent memories:\n" + ("\n".join(f"- {m}" for m in memories) or "- (none)") + "\n\n"
                f"A startup, {startup_text}.\n\n"
                "How do you feel today? mood: -1 = very worried or pessimistic, 0 = neutral, +1 = very optimistic "
                "(anxious or stressed means a NEGATIVE mood). In 1-2 first-person sentences, what is on your mind? "
                "What are you doing differently (spending, saving, investing)? Would you try the startup's offering, "
                "and why? How important are today's events to you (importance 0..1)?"
            )
            data, result = await runtime.ai.generate_json(
                AIRequest(system=PERSONA_SYSTEM, prompt=prompt, purpose="persona", schema=PERSONA_SCHEMA,
                          schema_name="persona_reaction", depth="fast", temperature=0.7),
                use_cache=False,
                validator=lambda d: isinstance(d, dict) and "mood" in d,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("persona %s failed: %s", key, exc)
            continue
        raw_mood = clamp(data.get("mood"), -10, 10)
        # some models answer on a 0..10 scale despite the instructions: map 0..10 onto -1..1
        mood = (raw_mood - 5) / 5 if raw_mood > 1 else clamp(raw_mood, -1, 1)
        importance = clamp(data.get("importance"), 0, 1, 0.5)
        actions = [str(a)[:400] for a in data.get("actions") or []][:4]
        content = (str(data.get("feeling") or "")[:400] + (" Doing: " + "; ".join(actions) if actions else "")).strip()
        vec, model = await runtime.embedder.embed_one(content)
        record = {
            "persona": desc.split(",")[0],
            "description": desc,
            "region": iso,
            "mood": mood,
            "feeling": data.get("feeling"),
            "actions": actions,
            "spending_intent": data.get("spending_intent"),
            "investing_intent": data.get("investing_intent"),
            "would_try_startup": data.get("would_try_startup"),
            "reason": data.get("reason"),
            "provider": result.provider_label,
        }
        async with SessionLocal() as session:
            session.add(PersonaMemory(persona_key=key, region=iso, kind="reaction", content=content,
                                      importance=importance, mood=mood, data=record, embedding=vec, embed_model=model))
            await session.commit()
        reactions.append(record)
        if progress:
            await progress(i / len(personas), f"{record['persona']} ({iso}) reacted, mood {mood:+.2f}")

    if not reactions:
        raise RuntimeError("no persona could react")
    by_region: dict[str, list[float]] = {}
    for r in reactions:
        by_region.setdefault(r["region"], []).append(r["mood"])
    region_mood = {k: round(sum(v) / len(v), 3) for k, v in by_region.items()}
    avg = sum(r["mood"] for r in reactions) / len(reactions)
    cutting = sum(1 for r in reactions if r["spending_intent"] == "cut")
    would = sum(1 for r in reactions if r["would_try_startup"] in ("yes", "maybe"))
    summary = (
        f"Average mood {avg:+.2f} across {len(reactions)} people in {len(region_mood)} regions; "
        f"{cutting} are cutting spending; {would} would consider the startup's offering. "
        + " ".join(f"{r['persona']} ({r['region']}): \"{(r['feeling'] or '')[:110]}\"" for r in reactions[:3])
    )
    await save_insight(
        kind="persona_pulse",
        title=f"How people are reacting today (mood {avg:+.2f})",
        summary=summary,
        content={"reactions": reactions, "region_mood": region_mood, "average_mood": round(avg, 3)},
        region="GLOBAL",
        severity="info",
        profile=profile,
        notify_level="info",
    )
    await set_state("persona_pulse", {"at": utcnow().isoformat(), "region_mood": region_mood})
    return {"personas": len(reactions), "average_mood": round(avg, 3)}
