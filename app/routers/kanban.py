"""Kanban 路由:CRUD + 阶段流转 + 备注。"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..modules import kanban
from .deps import templates

router = APIRouter()


@router.post("/api/kanban")
async def api_kanban_create(
    company: str = Form(...),
    role: str = Form(...),
    notes: str = Form(""),
):
    aid = kanban.create_application(company, role, notes)
    return JSONResponse({"ok": True, "id": aid})


@router.post("/kanban")
def page_kanban_create(
    request: Request,
    company: str = Form(...),
    role: str = Form(...),
    notes: str = Form(""),
):
    """浏览器 form submit 用,返回 HTML。"""
    kanban.create_application(company, role, notes)
    return RedirectResponse(url="/kanban", status_code=303)


@router.post("/api/kanban/{app_id}/stage")
def api_kanban_stage(app_id: int, stage: str = Form(...)):
    try:
        kanban.update_stage(app_id, stage)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "id": app_id, "stage": stage})


@router.post("/api/kanban/{app_id}/notes")
def api_kanban_notes(app_id: int, notes: str = Form(...)):
    kanban.update_notes(app_id, notes)
    return JSONResponse({"ok": True, "id": app_id})


@router.post("/api/kanban/{app_id}/delete")
def api_kanban_delete(app_id: int):
    kanban.delete_application(app_id)
    return JSONResponse({"ok": True, "id": app_id})
