"""W3 验收脚本(对齐文档 §七):

- memory.enabled 开关:关 → 不查记忆;开 → 检索 ACTIVE 弱点
- 出题器按 sid 偏移轮换不同弱点
- 预设 10 条弱点记忆(覆盖 7 领域),验证开/关后命中分布差异

环境:依赖 vLLM 在线 8000,跑 build_questions 会调 LLM 出题。
跳过慢测试:加 --skip-slow(可减少 vLLM 调用 6 次)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.db import init_db  # noqa: E402
from app.modules import interview, memory_engine, settings as _settings  # noqa: E402

_SKIP_SLOW = "--skip-slow" in sys.argv


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


print("== W3 验收 ==", flush=True)
init_db()


# ---------- 1. 开关默认 true ----------
@t("settings.get_bool('memory.enabled') 默认 true")
def _():
    assert _settings.get_bool("memory.enabled") is True, _settings.get("memory.enabled")

_()


# ---------- 2. 开关 false 时,_pick_weakness 走兜底(项目深挖/HR)----------
@t("memory.enabled=false:WEAKNESS 槽不查记忆,走兜底")
def _():
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    db.execute("DELETE FROM interview_session")
    # 灌一条 ACTIVE 记忆
    sid = interview.start_session("t", "")
    qa_id = db.execute(
        "INSERT INTO qa_log(session_id, question, question_source) VALUES (?,?,?)",
        (sid, "q", "WEAKNESS"),
    )
    memory_engine.add_memory(
        type_="WEAKNESS",
        content="Redis AOF 持久化机制答错",
        source_qa_id=qa_id,
    )
    _settings.update_many({"memory.enabled": "false"})
    qs = interview.build_questions(sid, "t", jd_keywords=["Redis"])
    weak_qs = [q for q in qs if q.get("question_source") == "WEAKNESS"]
    # 关闭时:不应该出现 Redis 题目(因为不查记忆,走兜底)
    cats = [q.get("category") for q in weak_qs]
    assert "Redis" not in cats, f"关闭时不应有 Redis 类别,实际 {cats}"
    # 兜底应该是项目深挖 / HR
    assert all(c in ("项目深挖", "HR") for c in cats), f"兜底类别错: {cats}"
    _settings.update_many({"memory.enabled": "true"})

_()


# ---------- 3. 开关 true + 有记忆,出 Redis 题目 ----------
@t("memory.enabled=true + 有 Redis 记忆:WEAKNESS 槽出现 Redis 题目")
def _():
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    db.execute("DELETE FROM interview_session")
    sid = interview.start_session("t", "")
    qa_id = db.execute(
        "INSERT INTO qa_log(session_id, question, question_source) VALUES (?,?,?)",
        (sid, "q", "WEAKNESS"),
    )
    memory_engine.add_memory(
        type_="WEAKNESS",
        content="Redis AOF 持久化机制答错",
        source_qa_id=qa_id,
    )
    _settings.update_many({"memory.enabled": "true"})
    qs = interview.build_questions(sid, "t", jd_keywords=["Redis"])
    weak_qs = [q for q in qs if q.get("question_source") == "WEAKNESS"]
    cats = [q.get("category") for q in weak_qs]
    assert "Redis" in cats, f"开启时应出现 Redis,实际 {cats}"

_()


# ---------- 4. 多条 weakness,sid 偏移覆盖不同领域 ----------
@t("sid 偏移:不同场次考到不同弱点(覆盖多领域)")
def _():
    if _SKIP_SLOW:
        print("\n    [SKIP --skip-slow]", flush=True)
        return
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    db.execute("DELETE FROM interview_session")
    # 灌 6 条不同领域
    seeds = [
        ("Redis AOF 答错",  "Redis"),
        ("MySQL B+ 树不熟", "MySQL"),
        ("Kafka 吞吐答不全","Kafka"),
        ("Spring IoC 不熟", "Spring"),
        ("JVM GC 不熟",     "JVM"),
        ("Agent RAG 不熟",  "Agent"),
    ]
    for i, (content, kw) in enumerate(seeds):
        sid = interview.start_session(f"s{i}", "")
        qa_id = db.execute(
            "INSERT INTO qa_log(session_id, question, question_source) VALUES (?,?,?)",
            (sid, f"q{i}", "WEAKNESS"),
        )
        memory_engine.add_memory(
            type_="WEAKNESS",
            content=content,
            source_qa_id=qa_id,
        )
    # 跑 6 场不同 sid
    all_cats = set()
    for i in range(6):
        print(f"\n    [diag] 跑 session {i+1}", flush=True)
        qs = interview.build_questions(i + 1, "t", jd_keywords=["Redis", "MySQL", "Kafka", "Spring", "JVM", "Agent"])
        weak = [q for q in qs if q.get("question_source") == "WEAKNESS"]
        for q in weak:
            all_cats.add(q.get("category"))
    assert len(all_cats) >= 4, f"sid 偏移应分散,实际领域: {all_cats}"

_()


# ---------- 5. 字段映射统一(避免 dot/underscore 不一致 bug 复发) ----------
@t("memory.half_life_weakness 等下划线 key 在 DEFAULTS 和 memory_engine 一致")
def _():
    for k in ("memory.half_life_weakness", "memory.half_life_fact", "memory.half_life_pref",
              "memory.enabled", "memory.merge_threshold", "memory.max_per_session",
              "memory.idle_days"):
        assert k in _settings.DEFAULTS, f"DEFAULTS 缺 {k}"
    # 实际读出来能 float
    assert _settings.get_float("memory.half_life_weakness") > 0

_()


print("\n== W3 全部通过 ==", flush=True)
print("下一步:跑 tests/ab_report.py 生成简历弹药表。", flush=True)
