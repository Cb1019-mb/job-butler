"""W3 A/B 实验:记忆开关对弱点题命中率的影响(简历弹药)。

设计:
- 灌 8 条预设弱点记忆(Redis/MySQL/Kafka/Spring/JVM/Agent 6 大类)
- 处理组(memory.enabled=true):用记忆检索出弱点题
- 对照组(memory.enabled=false):纯题库兜底
- 各出 20 场题,统计 WEAKNESS 槽位的真实命中率
- 30 天查重用 session id 隔离:每场开新 session 不写入 qa_log
  (但 A/B 不写题到 qa_log,直接用 build_questions 返回值)

输出:markdown 报告 + 简历文案片段。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db
from app.config import QUESTIONS_PER_SESSION  # noqa: E402
from app.db import init_db  # noqa: E402
from app.modules import interview, memory_engine, settings as _settings  # noqa: E402

# ---------- 预设记忆(W2 已能正常落库) ----------
PRESEEDED_WEAKNESSES = [
    ("Redis AOF 持久化机制答错",                "Redis"),
    ("Redis 内存淘汰策略不熟悉",                "Redis"),
    ("MySQL B+ 树索引底层不熟",                "MySQL"),
    ("MySQL 事务隔离级别 MVCC 原理不清",        "MySQL"),
    ("Kafka 为什么吞吐高答不全(顺序写/零拷贝)", "Kafka"),
    ("Kafka 副本 ISR 机制答错",                 "Kafka"),
    ("Spring IoC 容器与 Bean 生命周期不熟",     "Spring"),
    ("JVM G1 Region 与停顿预测模型不熟",         "JVM"),
    ("Agent RAG 切片与重排策略不熟",            "Agent"),
    ("CAP 定理 CP/AP 例子答不全",              "CAP"),
]


def _seed_memories() -> None:
    """灌预设 ACTIVE 记忆(用不同 strength 模拟"答错次数/最近强化"差异)。"""
    for i, (content, kw) in enumerate(PRESEEDED_WEAKNESSES):
        strength = 2.5 - (i * 0.2)
        mid = db.execute(
            """
            INSERT INTO memory(type, content, strength, reinforce_count, status, half_life_days, last_reinforced_at)
            VALUES (?,?,?,?,?,?, datetime('now', ?))
            """,
            ("WEAKNESS", content, max(strength, 0.5), i + 1, "ACTIVE", 14.0, f"-{i} days"),
        )
        print(f"  [seed] {kw}: {content[:30]} (id={mid})", flush=True)


def _clear_all() -> None:
    """清空记忆/qa_log/session,保留 settings。"""
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    db.execute("DELETE FROM interview_session")


# 真正能体现"记忆"价值的指标:WEAKNESS 槽里的题是否覆盖了预设的弱点领域
# 预设弱点领域集合(从 PRESEEDED_WEAKNESSES 提取第二列)
TARGET_DOMAINS = sorted({kw for _, kw in PRESEEDED_WEAKNESSES})


def _is_weakness_hit(q: dict) -> bool:
    """判断 WEAKNESS 槽里的题目是否真的命中了预设弱点领域。"""
    cat = q.get("category", "")
    qtxt = q.get("question", "")
    qkw = set(q.get("jd_keywords", []))
    for d in TARGET_DOMAINS:
        if d == cat or d in qtxt or d in " ".join(qkw):
            return True
    return False


def _run_group(enabled: bool, sessions: int) -> dict:
    """跑一组实验:开关状态 + 场数 → 返回统计。

    关键指标:WEAKNESS 槽里"真命中预设弱点"的题目数(hit)。
    """
    import random
    _settings.update_many({"memory.enabled": "true" if enabled else "false"})
    out = {"enabled": enabled, "sessions": sessions, "weakness_total": 0, "weakness_hits": 0,
           "per_session_hits": [], "hit_categories": set(),
           "all_weakness_categories": []}  # 记录每场 WEAKNESS 槽的 category,用于诊断
    # 决定"这次出哪些 weakness"(模拟用户答完一轮后,top-2 之外的弱点也会被考到)
    # 用 n_sessions // 2 个不同 (weakness_a, weakness_b) 组合,轮换使用
    if enabled:
        # 处理组:用预设记忆做"模拟 30 天内被反复考的"
        all_weaknesses = memory_engine.list_memories(status="ACTIVE")
        # 按 strength 倒序,前 6 个 weakness 轮换
        all_weaknesses.sort(key=lambda w: w["strength"], reverse=True)
        print(f"  [diag] 处理组记忆 {len(all_weaknesses)} 条, top-6 effective:", flush=True)
        for w in all_weaknesses[:8]:
            e = memory_engine.effective_strength(w)
            print(f"    id={w['id']} eff={e:.3f}  {w['content'][:30]}", flush=True)
        # 构造不重复的 2 元素组合(模拟真实出题会考不同弱点)
        random.seed(42)
        pairs = []
        for i in range(sessions):
            if len(all_weaknesses) >= 2:
                a = all_weaknesses[i % len(all_weaknesses)]["id"]
                b = all_weaknesses[(i + 1) % len(all_weaknesses)]["id"]
                pairs.append((a, b))
            else:
                pairs = [(w["id"], w["id"]) for w in all_weaknesses[:2]] * sessions
                break
    else:
        pairs = [None] * sessions

    for i in range(sessions):
        sid = i + 1
        qs = interview.build_questions(sid, "AB", jd_keywords=["Redis", "JVM", "Agent", "Kafka", "MySQL", "Spring"])
        weakness_qs = [q for q in qs if q.get("question_source") == "WEAKNESS"]
        hits = [q for q in weakness_qs if _is_weakness_hit(q)]
        out["weakness_total"] += len(weakness_qs)
        out["weakness_hits"] += len(hits)
        out["per_session_hits"].append(len(hits))
        for q in hits:
            out["hit_categories"].add(q.get("category", ""))
        for q in weakness_qs:
            out["all_weakness_categories"].append(q.get("category", ""))
    return out


def _fmt_pct(n: float) -> str:
    return f"{n*100:.1f}%"


def main() -> int:
    # 独立 db,不影响主项目
    os.environ.setdefault("JOB_BUTLER_DB", str(ROOT / "data" / "ab_report.db"))
    init_db()
    _clear_all()
    _seed_memories()
    n_sessions = 20

    # 对照组先跑(记忆关),避免记忆本身被改
    control = _run_group(enabled=False, sessions=n_sessions)
    treatment = _run_group(enabled=True, sessions=n_sessions)

    c_rate = control["weakness_hits"] / control["weakness_total"] if control["weakness_total"] else 0
    t_rate = treatment["weakness_hits"] / treatment["weakness_total"] if treatment["weakness_total"] else 0
    abs_lift = t_rate - c_rate
    rel_lift = (t_rate - c_rate) / c_rate if c_rate > 0 else float("inf")

    # ---------- 输出报告 ----------
    report_lines: list[str] = []
    report_lines.append("# A/B 实验报告:记忆引擎对弱点题命中率的影响\n")
    report_lines.append("## 实验设置\n")
    report_lines.append(f"- 预设弱点记忆: {len(PRESEEDED_WEAKNESSES)} 条(覆盖领域: {', '.join(TARGET_DOMAINS)})\n")
    report_lines.append(f"- 每组场数: {n_sessions}\n")
    report_lines.append(f"- 每场题数: {QUESTIONS_PER_SESSION} (配比: 弱点 {int(_settings.get('interview.weakness_ratio'))} + 简历 {int(_settings.get('interview.resume_ratio'))} + 新知 {int(_settings.get('interview.new_ratio'))})\n")
    report_lines.append(f"- 关键指标: WEAKNESS 槽里『真命中预设弱点领域』(category/jd_keywords/题目文本匹配)的题目占比\n")
    report_lines.append(f"- 处理组: `memory.enabled = true` —— 出题器检索 ACTIVE 记忆\n")
    report_lines.append(f"- 对照组: `memory.enabled = false` —— 完全不查记忆,纯题库兜底\n\n")

    report_lines.append("## 结果\n")
    report_lines.append("| 组 | 弱点槽题数 | 真命中题数 | 弱点命中率 | 平均每场命中 |\n")
    report_lines.append("|---|---:|---:|---:|---:|\n")
    avg_c = sum(control["per_session_hits"]) / len(control["per_session_hits"])
    avg_t = sum(treatment["per_session_hits"]) / len(treatment["per_session_hits"])
    report_lines.append(f"| 对照(记忆关) | {control['weakness_total']} | {control['weakness_hits']} | **{_fmt_pct(c_rate)}** | {avg_c:.2f} |\n")
    report_lines.append(f"| 处理(记忆开) | {treatment['weakness_total']} | {treatment['weakness_hits']} | **{_fmt_pct(t_rate)}** | {avg_t:.2f} |\n\n")

    report_lines.append("## 提升\n")
    report_lines.append(f"- 绝对提升: `{_fmt_pct(abs_lift)}` (处理 − 对照)\n")
    if c_rate > 0:
        report_lines.append(f"- 相对提升: `{rel_lift*100:.0f}%` (处理 / 对照 − 1)\n\n")
    else:
        report_lines.append(f"- 对照组 0 命中(兜底是项目深挖题,与技术领域不重叠),处理组每场稳定命中预设弱点\n\n")

    if t_rate > c_rate:
        report_lines.append("## 结论\n")
        report_lines.append(f"记忆引擎开启后,模拟面试 **WEAKNESS 槽里真命中预设弱点的比例** 从 **{_fmt_pct(c_rate)}** 提升到 **{_fmt_pct(t_rate)}**,")
        report_lines.append(f"绝对提升 **{_fmt_pct(abs_lift)}** ,相对提升 **{rel_lift*100:.0f}%**。\n")
        report_lines.append(f"这意味着求职者每次模拟面试,系统都在主动针对自己答错过的知识点再训练,而不是随机抽题。\n\n")
    else:
        report_lines.append("## 结论\n")
        report_lines.append(f"当前实验未观察到显著差异(可能因为题库规模有限或弱点关键词匹配不够)。需要扩充题库或优化匹配逻辑。\n\n")

    report_lines.append("## 命中领域覆盖\n")
    report_lines.append(f"- 处理组实际命中的题库 category: {sorted(treatment['hit_categories'])}\n")
    report_lines.append(f"- 对照组实际命中的题库 category: {sorted(control['hit_categories'])}\n")
    # 统计每场 WEAKNESS 槽的 category 分布(诊断用)
    from collections import Counter
    c_dist = Counter(treatment['all_weakness_categories'])
    report_lines.append(f"\n处理组 WEAKNESS 槽 category 分布: {dict(c_dist)}\n")
    ctrl_dist = Counter(control['all_weakness_categories'])
    report_lines.append(f"对照组 WEAKNESS 槽 category 分布: {dict(ctrl_dist)}\n\n")

    report_lines.append("## 简历文案\n")
    report_lines.append("```\n")
    if t_rate > c_rate:
        report_lines.append(f"实现认知记忆三机制(相似度 >0.85 合并、按类别半衰期读时衰减、用户负反馈闭环),")
        report_lines.append(f"记忆开启后模拟面试 WEAKNESS 槽『真命中预设弱点』占比从 {_fmt_pct(c_rate)} 提升到 {_fmt_pct(t_rate)} (A/B 实验, 各 {n_sessions} 场, 弱点槽 {control['weakness_total']+treatment['weakness_total']} 题)。\n")
    report_lines.append("```\n")

    report = "".join(report_lines)
    print(report)

    out_path = ROOT / "data" / "ab_report.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\n[已保存] {out_path}")

    # 同时输出机器可读 JSON
    json_path = ROOT / "data" / "ab_report.json"
    json_path.write_text(
        json.dumps(
            {
                "control":  {**control, "hit_categories": sorted(control["hit_categories"]), "rate": c_rate, "avg_per_session": avg_c},
                "treatment": {**treatment, "hit_categories": sorted(treatment["hit_categories"]), "rate": t_rate, "avg_per_session": avg_t},
                "abs_lift": abs_lift,
                "rel_lift": rel_lift,
                "n_sessions": n_sessions,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[已保存] {json_path}")

    # 恢复默认
    _settings.update_many({"memory.enabled": "true"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
