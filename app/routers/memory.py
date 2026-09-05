"""Memory 路由:列表 / 标记无效/已掌握/恢复 / 手动抽取。"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..modules import memory_engine

router = APIRouter()


@router.get("/api/memory")
def api_memory_list():
    rows = memory_engine.list_memories()
    for r in rows:
        r["effective"] = memory_engine.effective_strength(r)
        # embedding 是 BLOB,API 不返回原始字节(只标 has_embedding 标记)
        r["has_embedding"] = bool(r.pop("embedding", None))
    return JSONResponse(rows)


@router.post("/api/memory/{memory_id}/mark_invalid")
def api_memory_mark_invalid(memory_id: int):
    memory_engine.mark_invalid(memory_id)
    return JSONResponse({"ok": True, "id": memory_id, "status": "DISABLED"})


@router.post("/api/memory/{memory_id}/mark_mastered")
def api_memory_mark_mastered(memory_id: int):
    memory_engine.mark_mastered(memory_id)
    return JSONResponse({"ok": True, "id": memory_id, "status": "MASTERED"})


@router.post("/api/memory/{memory_id}/mark_active")
def api_memory_mark_active(memory_id: int):
    memory_engine.mark_active(memory_id)
    return JSONResponse({"ok": True, "id": memory_id, "status": "ACTIVE"})


@router.post("/api/interview/{session_id}/extract_memory")
def api_extract_memory(session_id: int):
    """手动触发:从已作答的 qa 抽记忆(正常 submit_answer 已自动跑)。"""
    stats = memory_engine.extract_and_store_for_session(session_id)
    return JSONResponse({"ok": True, "stats": stats})
