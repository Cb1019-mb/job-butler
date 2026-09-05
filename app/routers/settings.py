"""Settings 路由:读 / 改 / 重置 / 厂商预设 / tokens 统计。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..modules import settings as _settings
from .. import llm as _llm

log = logging.getLogger(__name__)
router = APIRouter()

# short key (前端 form 名) -> full key (DB 存)
_KEY_MAP: dict[str, str] = {
    "llm_provider":       "llm.provider",
    "llm_base_url":       "llm.base_url",
    "llm_api_key":        "llm.api_key",
    "llm_model":          "llm.model",
    "llm_temperature":    "llm.temperature",
    "llm_timeout":        "llm.timeout",
    "disable_thinking":   "llm.disable_thinking",
    "embed_base_url":     "embed.base_url",
    "embed_api_key":      "embed.api_key",
    "embed_model":        "embed.model",
    "embed_dim":          "embed.dim",
    "questions_per_session": "interview.questions_per_session",
    "weakness_ratio":        "interview.weakness_ratio",
    "resume_ratio":          "interview.resume_ratio",
    "new_ratio":             "interview.new_ratio",
    "dedup_days":            "interview.dedup_days",
    "merge_threshold":    "memory.merge_threshold",
    "max_per_session":    "memory.max_per_session",
    "half_life_weakness": "memory.half_life_weakness",
    "half_life_fact":     "memory.half_life_fact",
    "half_life_pref":     "memory.half_life_pref",
    "idle_days":          "memory.idle_days",
    "enabled":            "memory.enabled",
}


@router.get("/api/settings")
def api_settings_get():
    return JSONResponse(_settings.as_ui_dict())


@router.get("/api/settings/presets")
def api_settings_presets():
    return JSONResponse(_settings.provider_presets())


@router.get("/api/settings/tokens")
def api_settings_tokens():
    return JSONResponse(_settings.token_usage_stats())


@router.post("/api/settings")
async def api_settings_update(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body 必须是对象"}, status_code=400)
    flat: dict[str, str] = {}
    for group in body.values():
        if not isinstance(group, dict):
            continue
        for short, v in group.items():
            full = _KEY_MAP.get(short)
            if not full:
                continue
            # api_key 掩码值(****xxxx)代表用户未修改,跳过避免覆盖真实 key
            if short.endswith("api_key") and isinstance(v, str) and v.startswith("****"):
                continue
            flat[full] = str(v)
    _settings.update_many(flat)
    # LLM 客户端缓存清掉,下次请求按新配置重建
    _llm.reset_client()
    return JSONResponse({"ok": True, "updated": list(flat.keys())})


@router.post("/api/settings/reset")
def api_settings_reset():
    _settings.reset_to_defaults()
    _llm.reset_client()
    return JSONResponse({"ok": True, "settings": _settings.as_ui_dict()})
