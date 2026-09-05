"""MCP HTTP 入口(供 IDE Agent / 外部直接 HTTP 调用)。

FastMCP 三个工具的真源在 `app.mcp_server`;这里是包装成 HTTP GET 端点,方便不能用 stdio MCP 的客户端。
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..mcp_server import tool_memory_brief, tool_memory_search, tool_profile_get

router = APIRouter()


@router.get("/api/mcp/memory/search")
def api_mcp_memory_search(q: str = "", k: int = 5, status: str | None = "ACTIVE"):
    return JSONResponse(tool_memory_search(query=q, k=k, status=status))


@router.get("/api/mcp/memory/brief")
def api_mcp_memory_brief(top_n: int = 5):
    return JSONResponse(tool_memory_brief(top_n=top_n))


@router.get("/api/mcp/profile/get")
def api_mcp_profile_get(limit: int = 5):
    return JSONResponse(tool_profile_get(limit=limit))
