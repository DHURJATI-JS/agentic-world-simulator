"""First-run onboarding chat and the startup profile."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import config
from app.intel import profile as onboarding
from app.intel.common import load_profile
from app.runtime import runtime
from app.security import ai_rate_limit, require_user

router = APIRouter(prefix="/api", tags=["onboarding"])


@router.get("/profile")
async def get_profile(user: str = Depends(require_user)) -> dict:
    profile = await load_profile()
    complete = bool(profile and profile.get("onboarding_complete"))
    return {
        "profile": profile,
        "onboarding_complete": complete,
        "messages": [] if complete else await onboarding.get_messages(),
    }


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=config.CHAT_MAX_MESSAGE_CHARS)


@router.post("/onboarding/message")
async def chat(body: ChatBody, user: str = Depends(ai_rate_limit)) -> dict:
    try:
        return await onboarding.onboarding_turn(body.message)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


class ProfileBody(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field("", max_length=2000)
    sector: str = Field("", max_length=200)
    business_model: str = Field("", max_length=200)
    offerings: list[str] = Field(default_factory=list, max_length=30)
    sells_physical_products: bool = False
    offers_services: bool = False
    digital_only: bool = False
    can_ship_domestic: bool = False
    can_ship_international: bool = False
    ships_to: list[str] = Field(default_factory=list, max_length=60)
    target_markets: list[str] = Field(default_factory=list, max_length=60)
    hq_city: str = Field("", max_length=200)
    hq_country: str = Field("", max_length=100)
    stage: str = Field("", max_length=100)
    team_size: str = Field("", max_length=100)
    insured_for: list[str] = Field(default_factory=list, max_length=30)


@router.post("/onboarding/confirm")
async def confirm(body: ProfileBody, user: str = Depends(require_user)) -> dict:
    try:
        profile = await onboarding.save_profile(body.model_dump(), complete=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    await runtime.tasks.enqueue("risk.scan", {"force": True}, dedupe_key="sched:risk.scan", priority=2, requested_by=user)
    await runtime.hub.notify(
        "success", f"Profile saved: {profile['name']}",
        "Insights will now be filtered to what your startup can actually do; HQ location risk is being checked.",
        link="#profile",
    )
    runtime.hub.publish("profile", profile)
    return {"profile": profile}


@router.put("/profile")
async def update_profile(body: ProfileBody, user: str = Depends(require_user)) -> dict:
    return await confirm(body, user)


@router.post("/onboarding/reset")
async def reset(user: str = Depends(require_user)) -> dict:
    await onboarding.reset_onboarding()
    return {"ok": True, "messages": await onboarding.get_messages()}
