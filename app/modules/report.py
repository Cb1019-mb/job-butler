"""weekly_report 周报:从 qa_log + memory 统计,生成 Markdown。

触发:
- APScheduler 周日 23:59 定时
- 看板页"立即生成本周报告"按钮
- API `POST /api/report/generate`
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Any

from .. import db
from . import memory_engine

log = logging.getLogger(__name__)

# ---------- 知识点提取 ----------
TECH_KEYWORDS = [
    "Redis", "AOF", "RDB", "MySQL", "B+ 树", "索引", "事务", "MVCC", "InnoDB", "间隙锁",
    "Kafka", "消息队列", "分区", "副本", "ISR",
    "JVM", "GC", "G1", "CMS", "类加载", "双亲委派",
    "Spring", "IoC", "AOP", "Bean",
    "Python", "GIL", "装饰器", "生成器",
    "TCP", "HTTP", "HTTPS", "TLS", "三次握手", "四次挥手",
    "限流", "令牌桶", "漏桶", "缓存穿透", "雪崩", "击穿",
    "Agent", "RAG", "MCP", "Function Call", "LoRA", "RLHF", "DPO",
    "HashMap", "红黑树", "链表", "DP", "BFS", "DFS", "Dijkstra", "LIS",
    "CAP", "分布式",
]


def _extract_keywords(text: str) -> set[str]:
    out: set[str] = set()
    for kw in TECH_KEYWORDS:
        if kw in text:
            out.add(kw)
    return out


def _week_bounds(anchor: _dt.datetime | None = None) -> tuple[_dt.datetime, _dt.datetime]:
    """本周(周一 00:00 ~ 周日 23:59:59),返回本地时间(用于展示)。"""
    if anchor is None:
        anchor = _dt.datetime.now()
    monday = anchor - _dt.timedelta(days=anchor.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    next_monday = monday + _dt.timedelta(days=7)
    return monday, next_monday


def _to_utc_naive(dt_local: _dt.datetime) -> _dt.datetime:
    """本地 naive 时间 → UTC naive 时间。

    DB 里 created_at 由 SQLite `datetime('now')` 写入,是 UTC naive;
    直接用本地时间比较会偏移时区差(东八区 = 8 小时),导致周报漏算/多算数据。
    """
    return dt_local.astimezone(_dt.timezone.utc).replace(tzinfo=None)


def _week_label(monday: _dt.datetime) -> str:
    return monday.strftime("%Y-W%V")


# ---------- 统计 ----------

def _week_qa_stats(start: _dt.datetime, end: _dt.datetime) -> dict[str, Any]:
    # DB 存 UTC naive,查询边界先转换(见 _to_utc_naive)
    rows = db.fetch_all(
        """
        SELECT qa.id, qa.score_json, qa.question, qa.answer, s.created_at
        FROM qa_log qa JOIN interview_session s ON qa.session_id = s.id
        WHERE s.created_at >= ? AND s.created_at < ?
        ORDER BY qa.id
        """,
        (_to_utc_naive(start).isoformat(" "), _to_utc_naive(end).isoformat(" ")),
    )
    total = len(rows)
    scores: list[float] = []
    keyword_buckets: dict[str, list[int]] = {}
    for r in rows:
        try:
            sc = json.loads(r.get("score_json") or "{}")
        except Exception:
            sc = {}
        t = sc.get("tech_correctness", {}).get("score")
        c = sc.get("completeness", {}).get("score")
        s = sc.get("structure", {}).get("score")
        if all(isinstance(x, (int, float)) for x in (t, c, s)):
            scores.append((t + c + s) / 3.0)
        # 弱点的关键词命中
        try:
            tech = sc.get("tech_correctness", {}) or {}
            missed = tech.get("missed_points") or []
        except Exception:
            missed = []
        kws = _extract_keywords(r.get("question", "") + " " + r.get("answer", "") + " " + " ".join(missed))
        for kw in kws:
            keyword_buckets.setdefault(kw, []).append(r["id"])
    return {
        "qa_count": total,
        "scored_count": len(scores),
        "avg_score": (sum(scores) / len(scores)) if scores else None,
        "keyword_qa_ids": keyword_buckets,
    }


def _active_weaknesses_top(k: int = 5) -> list[dict[str, Any]]:
    rows = memory_engine.search_active_weaknesses(k=k)
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "content": r["content"],
            "effective": memory_engine.effective_strength(r),
            "strength": r["strength"],
            "reinforce_count": r["reinforce_count"],
        })
    return out


def _recent_progress(start: _dt.datetime, end: _dt.datetime) -> dict[str, Any]:
    """本周内记忆状态变化(粗略:数量差)。DB 存 UTC naive,边界先转换。"""
    start_utc, end_utc = _to_utc_naive(start).isoformat(" "), _to_utc_naive(end).isoformat(" ")
    # 本周新增
    new_rows = db.fetch_all(
        "SELECT COUNT(*) AS n FROM memory WHERE created_at >= ? AND created_at < ?",
        (start_utc, end_utc),
    )
    new_count = new_rows[0]["n"] if new_rows else 0
    # 本周变 MASTERED
    mastered = db.fetch_all(
        "SELECT COUNT(*) AS n FROM memory WHERE status='MASTERED' AND last_reinforced_at >= ? AND last_reinforced_at < ?",
        (start_utc, end_utc),
    )
    mastered_count = mastered[0]["n"] if mastered else 0
    # 本周变 DISABLED
    disabled = db.fetch_all(
        "SELECT COUNT(*) AS n FROM memory WHERE status='DISABLED' AND last_reinforced_at >= ? AND last_reinforced_at < ?",
        (start_utc, end_utc),
    )
    disabled_count = disabled[0]["n"] if disabled else 0
    return {
        "new_memories": new_count,
        "mastered": mastered_count,
        "disabled": disabled_count,
    }


# ---------- Markdown 模板 ----------

def render_markdown(week_start: _dt.datetime, week_end: _dt.datetime, *,
                    qa_stats: dict[str, Any], weaknesses: list[dict[str, Any]],
                    progress: dict[str, Any]) -> str:
    label = _week_label(week_start)
    avg = qa_stats["avg_score"]
    avg_str = f"{avg:.2f}/5.00" if avg is not None else "—"
    lines: list[str] = []
    lines.append(f"# 求职管家 · 成长周报 {label}\n")
    lines.append(f"周期: {week_start:%Y-%m-%d} (周一) ~ {week_end:%Y-%m-%d} (周日)\n")
    lines.append("## 本周概况\n")
    lines.append(f"- 模拟面试场次统计: **{qa_stats['qa_count']} 题**(已评分 {qa_stats['scored_count']})")
    lines.append(f"- 三轴平均分: **{avg_str}**")
    lines.append(f"- 新增记忆: **{progress['new_memories']}** 条")
    lines.append(f"- 标记掌握: **{progress['mastered']}** 条")
    lines.append(f"- 标记无效: **{progress['disabled']}** 条\n")

    lines.append("## 仍薄弱 Top 3\n")
    if weaknesses:
        for i, w in enumerate(weaknesses[:3], 1):
            content = w["content"]
            eff = w["effective"]
            lines.append(f"{i}. {content}  ·  *effective {eff:.2f} · 强度 {w['strength']} · 强化 {w['reinforce_count']} 次*")
    else:
        lines.append("- 当前没有 ACTIVE 记忆,继续保持! 🎯")
    lines.append("")

    lines.append("## 进步项\n")
    if progress["mastered"] > 0:
        lines.append(f"- 本周有 **{progress['mastered']}** 条记忆被标记为 MASTERED(连续答对 2 次)")
    else:
        lines.append("- 本周尚无已掌握项")
    lines.append("")

    lines.append("## 下周建议\n")
    if weaknesses:
        top = weaknesses[0]
        kw_set = _extract_keywords(top["content"])
        kw_hint = ", ".join(sorted(kw_set)) if kw_set else "相关知识点"
        lines.append(f"- 优先补齐 **{kw_hint}**(当前最薄弱)")
        lines.append(f"- 出题器下周会主动针对该弱点再出 2 道同领域题,集中攻克")
    else:
        lines.append("- 维持当前节奏,继续积累新场景下的弱点")
    lines.append("- 建议每天 1 场 5 题模拟面试,保持 30 天不间断")
    lines.append("")

    lines.append("---")
    lines.append(f"_本报告由求职管家自动生成 · {week_end:%Y-%m-%d %H:%M}_")
    return "\n".join(lines) + "\n"


# ---------- 入口 ----------

def generate(anchor: _dt.datetime | None = None, *, save: bool = True) -> dict[str, Any]:
    """生成周报,可选保存到 weekly_report 表。返回 {week_start, content_md, stats}。"""
    if anchor is None:
        anchor = _dt.datetime.now()
    week_start, week_end = _week_bounds(anchor)
    qa_stats = _week_qa_stats(week_start, week_end)
    weaknesses = _active_weaknesses_top(k=5)
    progress = _recent_progress(week_start, week_end)
    content = render_markdown(
        week_start, week_end,
        qa_stats=qa_stats, weaknesses=weaknesses, progress=progress,
    )
    stats = {
        "week_start": week_start.isoformat(" "),
        "week_end":   week_end.isoformat(" "),
        "qa_count":   qa_stats["qa_count"],
        "scored_count": qa_stats["scored_count"],
        "avg_score":  qa_stats["avg_score"],
        "new_memories": progress["new_memories"],
        "mastered":   progress["mastered"],
        "disabled":   progress["disabled"],
        "weakness_count": len(weaknesses),
    }
    result = {"week_start": week_start, "week_end": week_end, "content_md": content, "stats": stats}
    if save:
        cited_ids = [w["id"] for w in weaknesses]
        db.execute(
            """
            INSERT INTO weekly_report(week_start, content_md, memory_cited)
            VALUES (?, ?, ?)
            """,
            (week_start.strftime("%Y-%m-%d"), content, json.dumps(cited_ids, ensure_ascii=False)),
        )
    return result


def list_reports(limit: int = 12) -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT * FROM weekly_report ORDER BY week_start DESC LIMIT ?",
        (limit,),
    )


def get_report(week_start: str) -> dict[str, Any] | None:
    return db.fetch_one(
        "SELECT * FROM weekly_report WHERE week_start=?",
        (week_start,),
    )


def delete_report(report_id: int) -> None:
    db.execute("DELETE FROM weekly_report WHERE id=?", (report_id,))
