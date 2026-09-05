"""MCP 三个工具(memory.search / memory.brief / profile.get)。

允许外部 IDE Agent(Cursor / Claude Desktop)查询你的弱点库。
启动方式:在 main.py 里 import mcp_server,调 mcp_server.mcp.run(transport='stdio') 即可。
这里只暴露工具函数 + 工具注册;不直接 run(避免 uvicorn 启动时被阻塞)。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import db
from .modules import memory_engine

log = logging.getLogger(__name__)


def _memory_to_dict(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": m["id"],
        "type": m["type"],
        "content": m["content"],
        "status": m["status"],
        "strength": m.get("strength", 1.0),
        "effective": memory_engine.effective_strength(m),
        "reinforce_count": m.get("reinforce_count", 0),
        "source_qa_id": m.get("source_qa_id"),
        "created_at": m.get("created_at"),
    }


def tool_memory_search(query: str, k: int = 5, status: str | None = "ACTIVE") -> list[dict[str, Any]]:
    """搜索记忆:按关键词过滤 + effective 排序。

    简化版:LIKE 匹配 content;不调 embedding(避免外部依赖)。
    """
    rows = memory_engine.list_memories(status=status) if status else memory_engine.list_memories()
    if query:
        q = query.lower()
        rows = [r for r in rows if q in (r.get("content") or "").lower()]
    scored = [(r, memory_engine.effective_strength(r)) for r in rows]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [_memory_to_dict(r) for r, _ in scored[:k]]


def tool_memory_brief(top_n: int = 5) -> dict[str, Any]:
    """记忆简报:总数 + 各状态计数 + Top N 弱点。"""
    all_rows = memory_engine.list_memories()
    counts = {
        "ACTIVE":   sum(1 for r in all_rows if r["status"] == "ACTIVE"),
        "MASTERED": sum(1 for r in all_rows if r["status"] == "MASTERED"),
        "IDLE":     sum(1 for r in all_rows if r["status"] == "IDLE"),
        "DISABLED": sum(1 for r in all_rows if r["status"] == "DISABLED"),
    }
    top = tool_memory_search(query="", k=top_n, status="ACTIVE")
    return {
        "total": len(all_rows),
        "counts": counts,
        "top_weaknesses": top,
    }


def tool_profile_get(limit: int = 5) -> dict[str, Any]:
    """档案概要:最近 N 条档案的 id / 创建时间 / 抽取的 skills/jd_focus/weak_signals/projects。"""
    rows = db.fetch_all("SELECT * FROM profile ORDER BY id DESC LIMIT ?", (limit,))
    items = []
    for r in rows:
        try:
            skills = json.loads(r.get("parsed_skills_json") or "{}")
        except Exception:
            skills = {}
        items.append({
            "id": r["id"],
            "created_at": r.get("created_at"),
            "skills":     skills.get("skills", []),
            "jd_focus":   skills.get("jd_focus", []),
            "weak_signals": skills.get("weak_signals", []),
            "projects":   [
                {"name": p.get("name", ""), "summary": p.get("summary", "")}
                for p in (skills.get("projects") or [])
            ],
        })
    return {"profiles": items, "count": len(items)}


# ---------- MCP Server 适配器 ----------
# mcp 2.x 中 FastMCP 更名为 MCPServer;兼容两代 API,缺失时降级
try:
    try:
        from mcp.server.mcpserver import MCPServer as _Server  # mcp >= 2.0
    except ImportError:
        from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x

    mcp = _Server("job-butler")

    @mcp.tool()
    def memory_search(query: str, k: int = 5, status: str | None = "ACTIVE") -> list[dict[str, Any]]:
        """搜索求职管家的弱点记忆。

        Args:
            query: 关键词(空字符串返回 effective 最高的 N 条)
            k:     返回条数
            status: 限定状态(ACTIVE/MASTERED/IDLE/DISABLED);None 表示全部
        """
        return tool_memory_search(query=query, k=k, status=status)

    @mcp.tool()
    def memory_brief(top_n: int = 5) -> dict[str, Any]:
        """记忆简报:各状态计数 + Top N 活跃弱点。"""
        return tool_memory_brief(top_n=top_n)

    @mcp.tool()
    def profile_get(limit: int = 5) -> dict[str, Any]:
        """能力档案概要:最近 N 条档案的 skills / jd_focus / 疑似短板 / 项目。"""
        return tool_profile_get(limit=limit)

    MCP_READY = True

except Exception as e:  # pragma: no cover
    log.warning("FastMCP 不可用,MCP 工具将以降级形式提供: %s", e)
    mcp = None
    MCP_READY = False


def run_stdio() -> None:
    """以 stdio 方式启动 MCP server(供 Cursor / Claude Desktop 集成)。"""
    if not MCP_READY:
        raise RuntimeError("FastMCP 未安装;pip install mcp 后重试")
    mcp.run(transport="stdio")
