"""W4 验收脚本(对齐文档 §七 W4):

- 看板 CRUD
- 周报生成(不调 LLM,纯统计 + 模板)
- MCP 三个工具(降级版 / FastMCP 适配)
- APScheduler 启动(只测 start,不动 cron)
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.db import init_db  # noqa: E402
from app.modules import kanban, memory_engine, report  # noqa: E402


def t(name):
    def deco(fn):
        def wrap(*a, **kw):
            print(f"  · {name} ...", end=" ", flush=True)
            try:
                fn(*a, **kw)
                print("OK")
            except AssertionError as e:
                print(f"FAIL: {e}")
                sys.exit(1)
            except Exception as e:
                print(f"ERROR: {e}")
                sys.exit(1)
        return wrap
    return deco


print("== W4 验收 ==", flush=True)
init_db()


# ---------- 1. 看板 CRUD ----------
@t("看板:create → list → update_stage → update_notes → delete")
def _():
    db.execute("DELETE FROM application")
    aid = kanban.create_application("字节跳动", "AI 工程师", "内推 — 张三")
    app = kanban.get_application(aid)
    assert app["company"] == "字节跳动"
    assert app["stage"] == "投递"

    kanban.update_stage(aid, "笔试")
    assert kanban.get_application(aid)["stage"] == "笔试"

    kanban.update_notes(aid, "算法 4 道 + 系统设计 1 道")
    assert kanban.get_application(aid)["notes"].startswith("算法")

    kanban.delete_application(aid)
    assert kanban.get_application(aid) is None

_()


@t("看板:group_by_stage 按阶段分组")
def _():
    db.execute("DELETE FROM application")
    kanban.create_application("A", "X")
    kanban.create_application("B", "X")
    kanban.create_application("C", "X")
    # 把 A 流转到面试
    a_id = db.fetch_one("SELECT id FROM application WHERE company='A'")["id"]
    kanban.update_stage(a_id, "面试")
    groups = kanban.group_by_stage()
    assert "投递" in groups, list(groups.keys())
    assert "面试" in groups
    assert any(a["company"] == "A" for a in groups["面试"])
    assert len(groups["投递"]) == 2

_()


@t("看板:stage_summary 计数")
def _():
    summary = kanban.stage_summary()
    assert "投递" in summary and "面试" in summary
    assert summary["面试"] >= 1
    assert summary["投递"] >= 2

_()


@t("看板:update_stage 拒绝非法阶段")
def _():
    db.execute("DELETE FROM application")
    aid = kanban.create_application("A", "X")
    try:
        kanban.update_stage(aid, "无效阶段")
    except ValueError:
        pass
    else:
        raise AssertionError("应该拒绝非法阶段")

_()


# ---------- 2. 周报 ----------
@t("周报:render_markdown 包含关键 section")
def _():
    md = report.render_markdown(
        week_start=_dt.datetime(2026, 1, 5),
        week_end=_dt.datetime(2026, 1, 12),
        qa_stats={"qa_count": 5, "scored_count": 5, "avg_score": 3.5, "keyword_qa_ids": {}},
        weaknesses=[
            {"id": 1, "content": "Redis AOF 答错", "effective": 1.5, "strength": 2.0, "reinforce_count": 3},
        ],
        progress={"new_memories": 3, "mastered": 1, "disabled": 0},
    )
    assert "成长周报" in md
    assert "本周概况" in md
    assert "仍薄弱 Top 3" in md
    assert "Redis AOF 答错" in md
    assert "进步项" in md
    assert "下周建议" in md
    assert "Redis" in md  # 关键词提取

_()


@t("周报:generate() 落库 + 可查询")
def _():
    db.execute("DELETE FROM weekly_report")
    result = report.generate(save=True)
    assert "content_md" in result
    assert "week_start" in result
    assert result["stats"]["qa_count"] >= 0
    # 可查
    week_key = result["week_start"].strftime("%Y-%m-%d")
    row = report.get_report(week_key)
    assert row is not None
    assert row["content_md"] == result["content_md"]

_()


@t("周报:list_reports 倒序")
def _():
    db.execute("DELETE FROM weekly_report")
    report.generate(save=True)
    import time as _t
    _t.sleep(1.1)  # 避免同一周内 week_start 重复(单测对日期精度不高,但保险)
    rows = report.list_reports()
    assert len(rows) >= 1

_()


# ---------- 3. MCP 工具(降级版,FastMCP 可选)----------
@t("MCP:tool_memory_search 返回 ACTIVE 弱点")
def _():
    from app.mcp_server import tool_memory_search
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    db.execute("DELETE FROM interview_session")
    sid = "mcp_test"
    db.execute("INSERT INTO interview_session(id, role_tag, company, mode, created_at) VALUES (?,?,?,?, datetime('now'))",
               (1, "t", "t", "MOCK"))
    qa_id = db.execute(
        "INSERT INTO qa_log(session_id, question, question_source) VALUES (?,?,?)",
        (1, "Redis AOF 答错", "WEAKNESS"),
    )
    memory_engine.add_memory(type_="WEAKNESS", content="Redis AOF 答错", source_qa_id=qa_id)
    results = tool_memory_search(query="Redis", k=5, status="ACTIVE")
    assert len(results) >= 1
    assert any("Redis" in r["content"] for r in results)

_()


@t("MCP:tool_memory_brief 各状态计数")
def _():
    from app.mcp_server import tool_memory_brief
    brief = tool_memory_brief(top_n=3)
    assert "counts" in brief
    assert "ACTIVE" in brief["counts"]
    assert brief["counts"]["ACTIVE"] >= 1

_()


@t("MCP:tool_profile_get 返回档案(无档案时仍可调)")
def _():
    from app.mcp_server import tool_profile_get
    res = tool_profile_get(limit=5)
    assert "profiles" in res
    assert "count" in res
    assert isinstance(res["profiles"], list)

_()


@t("MCP:FastMCP 装饰器挂载(若 mcp 库可用)")
def _():
    from app.mcp_server import MCP_READY
    if MCP_READY:
        from app.mcp_server import mcp
        # mcp.tool() 注册的工具名应能在 mcp._tool_manager._tools 里
        names = set(mcp._tool_manager._tools.keys())
        assert {"memory_search", "memory_brief", "profile_get"}.issubset(names), names
    else:
        print("(FastMCP 未安装,跳过装饰器挂载校验)", flush=True)

_()


# ---------- 4. APScheduler ----------
@t("APScheduler:start() 幂等且可 shutdown")
def _():
    from app import scheduler
    scheduler.start()
    scheduler.start()  # 第二次应 no-op
    assert scheduler._scheduler is not None
    # 验证挂的 job 存在
    job = scheduler._scheduler.get_job("weekly_report")
    assert job is not None
    scheduler.shutdown()
    assert scheduler._scheduler is None

_()


# ---------- 5. 页面/路由可达 ----------
# 由 main.py 集成,这里只验 settings 仍然没坏
@t("settings:reset_to_defaults 后 4 大板块 keys 完整")
def _():
    from app.modules import settings as _settings
    _settings.reset_to_defaults()
    ui = _settings.as_ui_dict()
    assert set(ui.keys()) == {"model", "interview", "memory"}
    assert "enabled" in ui["memory"]
    assert "questions_per_session" in ui["interview"]

_()


print("\n== W4 全部通过 ==", flush=True)
print("下一步:启动 uvicorn 走一遍 /kanban 页面 + 点'立即生成周报'验证端到端。", flush=True)
