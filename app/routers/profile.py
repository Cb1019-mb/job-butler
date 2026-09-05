"""Profile 路由:档案解析(zip + 文本)/ 详情 / 删除。"""
from __future__ import annotations

import json
import logging
import zipfile

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from ..modules import profile
from .deps import resolve_profile_id

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/profile/parse")
def api_profile_parse(
    resume: str = Form(...),
    jd: str = Form(...),
    name: str = Form(""),
):
    """浏览器 form submit 入口:解析完重定向到档案页。"""
    profile.create_or_update(resume, jd, name)
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/api/profile/upload")
async def api_profile_upload(
    zip_file: UploadFile = File(...),
    jd: str = Form(""),
    name: str = Form(""),
):
    """zip 上传:解压 → 合并文本 → 入库(等同粘贴简历)。"""
    from .. import db as _db
    raw = await zip_file.read()
    if not raw:
        return JSONResponse({"error": "空文件"}, status_code=400)
    try:
        merged, skipped = profile.extract_zip_text(raw)
    except zipfile.BadZipFile:
        return JSONResponse({"error": "不是合法的 zip 文件"}, status_code=400)
    if not merged:
        return JSONResponse(
            {"error": "zip 中未发现可解析的文本文件", "skipped": skipped},
            status_code=400,
        )
    pid = profile.create_or_update(merged, jd, name)
    return JSONResponse(
        {
            "ok": True,
            "id": pid,
            "char_count": len(merged),
            "skipped": skipped,
            "note": profile.BINARY_SUFFIXES_NOTE,
            "merged": merged,  # W4 修复 #5:让前端能回显真实解压内容
        }
    )


@router.post("/api/profile/{profile_id}/delete")
def api_profile_delete(profile_id: int):
    profile.delete_profile(profile_id)
    return JSONResponse({"ok": True, "id": profile_id})


@router.post("/profile/{profile_id}/delete")
def page_profile_delete(profile_id: int):
    """浏览器 form submit 版删除,返回 HTML 重定向。"""
    profile.delete_profile(profile_id)
    return RedirectResponse(url="/profile", status_code=303)


# 公共导出:被 interview.py router 复用(parse 时读档案 jd_focus)
__all__ = ["router", "resolve_profile_id"]
