"""Weekly Report 路由:生成 / 列表 / 删除。"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, RedirectResponse

from ..modules import report

router = APIRouter()


@router.post("/api/report/generate")
def api_report_generate():
    result = report.generate(save=True)
    return JSONResponse({
        "ok": True,
        "week_start": result["week_start"].strftime("%Y-%m-%d"),
        "content_md": result["content_md"],
        "stats": result["stats"],
    })


@router.get("/api/report/list")
def api_report_list():
    rows = report.list_reports(limit=20)
    return JSONResponse(rows)


@router.post("/api/report/{report_id}/delete")
def api_report_delete(report_id: int):
    report.delete_report(report_id)
    return JSONResponse({"ok": True, "id": report_id})


@router.post("/report/{report_id}/delete")
def page_report_delete(report_id: int):
    """浏览器 form submit 版删除周报。"""
    report.delete_report(report_id)
    return RedirectResponse(url="/kanban", status_code=303)
