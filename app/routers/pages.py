"""6 个页面路由:首页 / 档案 / 模拟面试 / 记忆时间线 / 看板+周报 / 设置。"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import db
from ..modules import interview, kanban, memory_engine, profile, report
from ..modules import settings as _settings
from .deps import templates

log = logging.getLogger(__name__)
router = APIRouter()


# ---------- 6 个页面 ----------

@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html",
        {"request": request, "active": "index"},
    )


@router.get("/profile", response_class=HTMLResponse)
def page_profile(request: Request):
    p = profile.get_latest()
    return templates.TemplateResponse(
        request, "profile.html",
        {
            "request": request, "profile": p, "active": "profile",
            "profiles": profile.list_profiles(limit=30),
        },
    )


@router.get("/profile/{profile_id}", response_class=HTMLResponse)
def page_profile_detail(request: Request, profile_id: int):
    p = profile.get_profile(profile_id)
    if not p:
        return HTMLResponse("<h1>档案不存在</h1>", status_code=404)
    return templates.TemplateResponse(
        request, "profile.html",
        {
            "request": request, "profile": p, "active": "profile",
            "profiles": profile.list_profiles(limit=30),
        },
    )


@router.get("/interview", response_class=HTMLResponse)
def page_interview(request: Request):
    sessions = interview.list_sessions(limit=10)
    return templates.TemplateResponse(
        request, "interview.html",
        {
            "request": request, "sessions": sessions, "current_session": None, "questions": [],
            "profiles": profile.list_profiles(limit=30), "active": "interview",
        },
    )


@router.get("/timeline", response_class=HTMLResponse)
def page_timeline(request: Request):
    memories = memory_engine.list_memories()
    # 计算 effective
    for m in memories:
        m["effective"] = memory_engine.effective_strength(m)
        # W4 修复 D 续:从 qa_log 反查 session_id(给 "查看原题" 链接用)
        if m.get("source_qa_id"):
            qa = interview.get_qa_log(m["source_qa_id"])
            m["session_id"] = qa["session_id"] if qa else None
        else:
            m["session_id"] = None
    counts = {
        "ACTIVE":   sum(1 for m in memories if m["status"] == "ACTIVE"),
        "MASTERED": sum(1 for m in memories if m["status"] == "MASTERED"),
        "IDLE":     sum(1 for m in memories if m["status"] == "IDLE"),
        "DISABLED": sum(1 for m in memories if m["status"] == "DISABLED"),
    }
    return templates.TemplateResponse(
        request, "timeline.html",
        {"request": request, "active": "timeline", "memories": memories, "counts": counts},
    )


# W4 修复 #14:会话历史页(原来是 JSON API,浏览器看到 JSON 原文)
@router.get("/interview/{session_id}", response_class=HTMLResponse)
def page_session_detail(request: Request, session_id: int):
    session = db.fetch_one("SELECT * FROM interview_session WHERE id=?", (session_id,))
    if not session:
        return HTMLResponse("<h1>会话不存在</h1>", status_code=404)
    qa_rows = interview.get_qa_logs(session_id)
    for r in qa_rows:
        try:
            r["score"] = json.loads(r.get("score_json") or "{}")
        except Exception:
            r["score"] = {}
    return templates.TemplateResponse(
        request, "session_detail.html",
        {
            "request": request, "active": "interview",
            "session": session, "qa_rows": qa_rows,
        },
    )


@router.get("/kanban", response_class=HTMLResponse)
def page_kanban(request: Request):
    groups = kanban.group_by_stage()
    summary = kanban.stage_summary()
    reports = report.list_reports(limit=12)
    return templates.TemplateResponse(
        request, "kanban.html",
        {
            "request": request, "active": "kanban",
            "groups": groups, "summary": summary, "reports": reports,
            "stages": kanban.STAGES,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request):
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "request": request, "active": "settings",
            "ui": _settings.as_ui_dict(),
            "presets": _settings.provider_presets(),
            "tokens": _settings.token_usage_stats(),
        },
    )
