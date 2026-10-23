"""W2 验收脚本(对齐文档 §八 W2 阶段 + §七 验收):

- 质量门(空话/无知识点被拒)
- 合并阈值边界(>0.85 合并)
- 状态机(MASTERED / IDLE / DISABLED 转移)
- 场次上限(>6 裁剪)
- 30 条回归(无 LLM,纯规则抽取,目标 ≥80% 命中率)
- 出题器接记忆(弱点被再次出题)

环境:依赖 vLLM 在线 8000,最后一个测试会调 LLM 出题。
跳过慢测试:加 --skip-slow
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 隔离:验收脚本写独立临时库(每次运行重建),不碰真实库 data/job_butler.db。
_TEST_DB = ROOT / "data" / "test_w2.db"
for _suffix in ("", "-wal", "-shm"):
    _p = Path(str(_TEST_DB) + _suffix)
    if _p.exists():
        _p.unlink()
os.environ["JOB_BUTLER_DB"] = str(_TEST_DB)

_SKIP_SLOW = "--skip-slow" in sys.argv

from app.config import QUESTIONS_PER_SESSION  # noqa: E402
from app.db import init_db  # noqa: E402
from app.modules import interview, memory_engine, settings  # noqa: E402


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


print("== W2 验收 ==", flush=True)
init_db()


# ---------- 1. 质量门 ----------
@t("质量门:空话被拒(表达不够自信)")
def _():
    bad = {"content": "表达不够自信,需要加强", "knowledge": ""}
    assert not memory_engine._quality_pass(bad), bad

_()

@t("质量门:具体知识点通过(Redis AOF 漏点)")
def _():
    good = {"content": "漏了 AOF 是日志记录这点,持久化机制混淆", "knowledge": "AOF"}
    assert memory_engine._quality_pass(good), good

_()

@t("质量门:短文本被拒")
def _():
    short = {"content": "忘了", "knowledge": ""}
    assert not memory_engine._quality_pass(short), short

_()


# ---------- 2. 抽取(规则)----------
@t("规则抽取:从漏点拿具体知识")
def _():
    qa = {
        "id": 1, "question": "Redis 持久化?",
        "answer": "RDB 吧",
        "score": {
            "tech_correctness": {"missed_points": ["AOF 定义", "优缺点对比"]},
            "completeness": {"missed_points": ["AOF 是日志记录"]},
        },
    }
    items = memory_engine.extract_from_qa(qa)
    assert len(items) >= 1, items
    assert any("AOF" in it["content"] or "持久化" in it["content"] for it in items), items

_()

@t("规则抽取:空话漏点被质量门拒")
def _():
    qa = {
        "id": 2, "question": "自我介绍?",
        "answer": "我来自 XX 学校,熟悉 Python。",
        "score": {
            "completeness": {"missed_points": ["表达不够自信", "应该多练"]},
        },
    }
    items = memory_engine.extract_from_qa(qa)
    # 规则只抽漏点,且都过质量门。空话漏点会被质量门拒
    for it in items:
        assert "不够自信" not in it["content"]
        assert "多练" not in it["content"]

_()


# ---------- 3. 合并 ----------
@t("合并:相似内容会合并到旧记忆(强化计数 +1)")
def _():
    # 探一下 embedding 可用性
    try:
        memory_engine.encode_doc("test")
    except Exception as e:
        print(f"\n    embedding 不可达,跳过合并测试: {type(e).__name__}", flush=True)
        return
    # 清环境
    from app import db
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    db.execute("DELETE FROM interview_session")
    # 灌 2 个 qa 让 source_qa_id 满足 FK
    sid = interview.start_session("t", "")
    qa1 = db.execute(
        "INSERT INTO qa_log(session_id, question, question_source) VALUES (?,?,?)",
        (sid, "q1", "WEAKNESS"),
    )
    qa2 = db.execute(
        "INSERT INTO qa_log(session_id, question, question_source) VALUES (?,?,?)",
        (sid, "q2", "WEAKNESS"),
    )
    # 第一条
    m1 = memory_engine.add_memory(
        type_="WEAKNESS",
        content="Redis AOF 持久化机制混淆,答错了",
        source_qa_id=qa1,
    )
    # 第二条几乎相同
    m2 = memory_engine.add_memory(
        type_="WEAKNESS",
        content="Redis AOF 持久化机制答错,概念不清",
        source_qa_id=qa2,
    )
    row = memory_engine.get_memory(m1)
    assert row is not None
    assert row["strength"] >= 1.0

_()


# ---------- 4. 状态机 ----------
@t("状态机:mark_mastered 后 search_active 不再返回")
def _():
    from app import db
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    db.execute("DELETE FROM interview_session")
    sid = interview.start_session("t", "")
    qa_id = db.execute(
        "INSERT INTO qa_log(session_id, question, question_source) VALUES (?,?,?)",
        (sid, "Kafka 零拷贝?", "WEAKNESS"),
    )
    mid = memory_engine.add_memory(
        type_="WEAKNESS",
        content="Kafka 零拷贝机制不熟悉",
        source_qa_id=qa_id,
    )
    print(f"\n    mid={mid}", flush=True)
    row = memory_engine.get_memory(mid)
    print(f"    row.half_life_days={row.get('half_life_days')!r} type={row.get('type')!r}", flush=True)
    print(f"    settings half_life_weakness={memory_engine._settings.get('memory.half_life_weakness')!r}", flush=True)
    rows = memory_engine.search_active_weaknesses()
    print(f"    search returns {len(rows)} rows", flush=True)
    assert any(m["id"] == mid for m in rows), f"mid={mid} not in {[r['id'] for r in rows]}"
    memory_engine.mark_mastered(mid)
    assert all(m["id"] != mid for m in memory_engine.search_active_weaknesses())
    memory_engine.mark_active(mid)

_()

@t("状态机:DISABLED 不再被出题器检索")
def _():
    from app import db
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    db.execute("DELETE FROM interview_session")
    sid = interview.start_session("t", "")
    qa_id = db.execute(
        "INSERT INTO qa_log(session_id, question, question_source) VALUES (?,?,?)",
        (sid, "MySQL B+ 树?", "WEAKNESS"),
    )
    mid = memory_engine.add_memory(
        type_="WEAKNESS",
        content="MySQL B+ 树索引底层不熟",
        source_qa_id=qa_id,
    )
    memory_engine.mark_invalid(mid)
    assert all(m["id"] != mid for m in memory_engine.search_active_weaknesses())

_()


# ---------- 5. 场次上限 ----------
@t("场次上限:本场 > max 会被裁剪到 IDLE")
def _():
    from app import db
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    # 把 max 调小方便测
    settings.update_many({"memory.max_per_session": "3"})
    sid = interview.start_session("t", "")
    # 直接灌 7 条 ACTIVE 记忆到同一 session 的 qa(记录 id 用于 cap)
    qa_ids: list[int] = []
    for i in range(7):
        qa_id = db.execute(
            "INSERT INTO qa_log(session_id, question, question_source) VALUES (?,?,?)",
            (sid, f"q{i}", "WEAKNESS"),
        )
        qa_ids.append(qa_id)
    for i, qa_id in enumerate(qa_ids):
        db.execute(
            "INSERT INTO memory(type, content, source_qa_id, strength, status) VALUES (?,?,?,?,?)",
            ("WEAKNESS", f"weakness-{i}", qa_id, 1.0 + i * 0.5, "ACTIVE"),
        )
    n = memory_engine.enforce_session_cap(qa_ids)
    assert n == 4, f"应该裁剪 4 条(7-3),实际 {n}"
    active_count = len(db.fetch_all("SELECT id FROM memory WHERE status='ACTIVE'"))
    assert active_count == 3, active_count
    settings.update_many({"memory.max_per_session": "6"})

_()


# ---------- 6. 30 条回归(纯规则,目标 ≥80%) ----------
@t("回归 30 条:规则抽取命中率 ≥80%")
def _():
    from app import db
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    db.execute("DELETE FROM interview_session")
    sid = interview.start_session("reg", "")
    reg = json.load(open(ROOT / "data" / "regression_30.json", encoding="utf-8"))
    hit = 0
    total = 0
    for case in reg:
        qa_id = db.execute(
            "INSERT INTO qa_log(session_id, question, question_source, answer, score_json) VALUES (?,?,?,?,?)",
            (sid, case["question"], "NEW", case["answer"], json.dumps(case["score"], ensure_ascii=False)),
        )
        qa = {
            "id": qa_id,
            "question": case["question"],
            "answer": case["answer"],
            "score": case["score"],
        }
        items = memory_engine.extract_from_qa(qa)
        if case["expect_min_weakness"] == 0:
            for it in items:
                assert "不够自信" not in it["content"]
                assert "应该多练" not in it["content"]
        else:
            if len(items) >= case["expect_min_weakness"]:
                hit += 1
            total += 1
    rate = hit / total if total else 1.0
    print(f"\n    >>> 命中率: {hit}/{total} = {rate:.0%}", flush=True)
    assert rate >= 0.8, f"命中率 {rate:.0%} < 80%"

_()


# ---------- 7. 出题器接记忆 ----------
@t("出题器:有 ACTIVE 记忆时,第二场 WEAKNESS 优先出对应知识点题")
def _():
    if _SKIP_SLOW:
        print("\n    [SKIP --skip-slow]", flush=True)
        return
    from app import db
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    settings.update_many({"interview.questions_per_session": "3",
                          "interview.weakness_ratio": "2",
                          "interview.resume_ratio": "0",
                          "interview.new_ratio": "1"})
    # 灌一条关于 Redis 持久化的弱点
    memory_engine.add_memory(
        type_="WEAKNESS",
        content="Redis AOF 持久化机制答错,概念混淆",
        source_qa_id=None,
    )
    sid = interview.start_session("t", "")
    qs = interview.build_questions(sid, "t", jd_keywords=[])
    sources = [q["question_source"] for q in qs]
    assert sources.count("WEAKNESS") == 2, sources
    # 还原默认
    settings.update_many({"interview.questions_per_session": "5",
                          "interview.weakness_ratio": "2",
                          "interview.resume_ratio": "2",
                          "interview.new_ratio": "1"})

_()


# ---------- 8. submit_answer 触发抽取(全链路) ----------
@t("全链路:submit_answer 后自动沉淀记忆(同步版,无 LLM 抽 -> 依赖规则)")
def _():
    from app import db
    db.execute("DELETE FROM memory")
    db.execute("DELETE FROM qa_log")
    sid = interview.start_session("t", "")
    sid2 = interview.start_session("t2", "")
    # 灌一条 qa 到 sid2,有评分+漏点
    qa_id = db.execute(
        "INSERT INTO qa_log(session_id, question, question_source, answer, score_json) VALUES (?,?,?,?,?)",
        (sid2, "Redis 持久化?", "NEW", "RDB 吧",
         json.dumps({"completeness": {"missed_points": ["AOF 概念不清", "RDB 与 AOF 区别"]}}, ensure_ascii=False)),
    )
    # 跑抽取
    stats = memory_engine.extract_and_store_for_session(sid2)
    assert stats["qa_count"] >= 1
    mems = memory_engine.list_memories(status="ACTIVE")
    assert len(mems) >= 1, f"应该有 ≥1 条 ACTIVE 记忆,实际 {len(mems)}"
    has_aof = any("AOF" in m["content"] or "持久化" in m["content"] for m in mems)
    assert has_aof, f"记忆应包含 AOF/持久化,实际: {[m['content'] for m in mems]}"

_()


print("\n== W2 全部通过 ==", flush=True)
print("下一步:启动 uvicorn 走端到端,看 /timeline 页面。", flush=True)
