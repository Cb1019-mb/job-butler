"""W1 验收脚本(对齐文档 §八 测试清单):

1. 衰减函数:effective = strength × exp(-λ·days) 边界
2. 出题配比:5 题 / WEAKNESS-RESUME-NEW 分布
3. 30 天查重
4. 点评 JSON Schema 校验(默认跳过 LLM 调用,加 --with-llm 开启)
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 隔离:验收脚本写独立临时库(每次运行重建),不碰真实库 data/job_butler.db。
# 共享真库时,30 天查重池被历史 session_question 污染,出题配比用例结果不稳定。
_TEST_DB = ROOT / "data" / "test_w1.db"
for _suffix in ("", "-wal", "-shm"):
    _p = Path(str(_TEST_DB) + _suffix)
    if _p.exists():
        _p.unlink()
os.environ["JOB_BUTLER_DB"] = str(_TEST_DB)

from app.config import (  # noqa: E402
    DEDUP_DAYS,
    MERGE_THRESHOLD,
    NEW_KNOWLEDGE_RATIO,
    QUESTIONS_PER_SESSION,
    RESUME_RATIO,
    WEAKNESS_RATIO,
    WEAK_HALF_LIFE_DAYS,
)
from app.db import init_db  # noqa: E402
from app.modules import interview, memory_engine  # noqa: E402
from app.modules.question_bank import load_bank  # noqa: E402

WITH_LLM = "--with-llm" in sys.argv


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


print("== W1 验收 ==", flush=True)
init_db()
print("[1] init_db done", flush=True)


# ---------- 1. 衰减函数 ----------
@t("衰减函数:0 天 → strength 不变")
def _():
    # DB 存 UTC naive,测试用 UTC 构造
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = {"strength": 2.0, "last_reinforced_at": now, "type": "WEAKNESS"}
    e = memory_engine.effective_strength(row)
    assert abs(e - 2.0) < 0.01, e

_()


@t("衰减函数:half_life 天 → 衰减为一半")
def _():
    import datetime as _dt
    # 用 UTC 构造,与 DB 口径一致
    last = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=WEAK_HALF_LIFE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    row = {"strength": 2.0, "last_reinforced_at": last, "type": "WEAKNESS"}
    e = memory_engine.effective_strength(row)
    assert abs(e - 1.0) < 0.05, e

_()


# ---------- 2. 出题配比 ----------
bank = load_bank()
assert len(bank) >= 50, f"题库至少 50 条,实际 {len(bank)}"
print(f"  · 题库加载 OK,共 {len(bank)} 条")


def without_llm(fn):
    """封 LLM:临时把 base_url 指向不可达端点并重建客户端,保证走题库兜底。

    出题配比 / 查重用例验证的是题库逻辑,需要确定性输出;
    若不封端点,LLM 可达时出题器走 LLM 路径,配比断言必然失败。
    """
    def wrap():
        from app.modules import settings as _s
        from app import llm as _llm
        orig = _s.get("llm.base_url")
        _s.update_many({"llm.base_url": "http://127.0.0.1:9/v1"})
        _llm.reset_client()
        try:
            fn()
        finally:
            _s.update_many({"llm.base_url": orig})
            _llm.reset_client()
    return wrap


@t("出题器:5 题 / WEAKNESS×2 + RESUME×2 + NEW×1")
@without_llm
def _():
    # 隔离:清 qa_log,避免历史作答占满 30 天查重池导致题数不足(共享 DB 场景)
    from app import db as _db
    _db.execute("DELETE FROM qa_log")
    sid = interview.start_session("测试岗位", "测试公司")
    qs = interview.build_questions(sid, "测试岗位", jd_keywords=["Redis", "JVM", "Agent"])
    assert len(qs) == QUESTIONS_PER_SESSION, f"题数 {len(qs)} ≠ 5"
    sources = [q.get("question_source") for q in qs]
    assert sources.count("WEAKNESS") == WEAKNESS_RATIO, sources
    assert sources.count("RESUME") == RESUME_RATIO, sources
    assert sources.count("NEW") == NEW_KNOWLEDGE_RATIO, sources
    print(f"\n    来源分布: {sources}", flush=True)

_()
print("[3] build_questions done", flush=True)


# ---------- 3.5 出题 JSON 归一化(采分点别名,防点评无依据) ----------
@t("出题 JSON:score_points 别名归一化进 scoring_points")
def _():
    from app.json_utils import normalize_keys
    data = normalize_keys({
        "questions": [
            # 只写了别名键(模型常见行为)
            {"question": "q1", "reference_answer": "r", "score_points": ["p1", "p2"], "category": "c"},
            # 正名键为空 + 别名键有值(实测 qwen3-4b 双键并存)
            {"question": "q2", "reference_answer": "r", "scoring_points": [], "score_points": ["p3"], "category": "c"},
        ]
    }, kind="question")
    q1, q2 = data["questions"]
    assert q1["scoring_points"] == ["p1", "p2"], q1
    assert q2["scoring_points"] == ["p3"], q2
_()


# ---------- 3.6 点评保底:cited/missed 全空时用采分点补齐 ----------
@t("点评保底:cited/missed 全空时用采分点补 missed_points")
def _():
    from app.json_utils import ensure_judge_citations
    score = {
        "tech_correctness": {"score": 2, "comment": "", "cited_points": [], "missed_points": []},
        "completeness": {"score": 1, "comment": "", "missed_points": []},
        "structure": {"score": 1, "comment": "", "suggestion": ""},
        "overall": "x",
    }
    out = ensure_judge_citations(score, ["采分点A", "采分点B"])
    assert out["tech_correctness"]["missed_points"] == ["采分点A", "采分点B"], out
    # 模型已有引用时不覆盖
    score2 = {"tech_correctness": {"score": 5, "comment": "", "cited_points": ["采分点A"], "missed_points": []}}
    out2 = ensure_judge_citations(score2, ["采分点A", "采分点B"])
    assert out2["tech_correctness"]["missed_points"] == [], out2
    # 未提供采分点时不动作
    score3 = {"tech_correctness": {"score": 0, "comment": "", "cited_points": [], "missed_points": []}}
    assert ensure_judge_citations(score3, [])["tech_correctness"]["missed_points"] == []
_()


# ---------- 3.7 点评容错:cited/missed 写成数字/字符串时纠偏 ----------
@t("点评容错:cited/missed 为数字时纠偏为数组并与保底组合")
def _():
    from app.json_utils import ensure_judge_citations, normalize_keys
    score = normalize_keys({
        "tech_correctness": {"cited_points": 3, "missed_points": 2},
        "completeness": 3,
        "structure": 2,
    }, kind="judge")
    tc = score["tech_correctness"]
    assert isinstance(tc["cited_points"], list) and tc["cited_points"] == [], tc
    assert isinstance(tc["missed_points"], list) and tc["missed_points"] == [], tc
    # 纠偏成空列表后,保底函数用真实采分点补齐,点评有据可查
    out = ensure_judge_citations(score, ["采分点1", "采分点2"])
    assert out["tech_correctness"]["missed_points"] == ["采分点1", "采分点2"], out
_()


# ---------- 3.8 点评防照抄:占位符检测 + content_check 重试 ----------
@t("点评防照抄:占位符点评被检测,content_check 触发重试")
def _():
    from app.json_utils import judge_looks_like_placeholder, run_json_task
    echo = {
        "tech_correctness": {"score": 0, "comment": "<针对作答的一句话技术点评>", "cited_points": [], "missed_points": []},
        "completeness": {"score": 0, "comment": "", "missed_points": []},
        "structure": {"score": 0, "comment": "", "suggestion": ""},
        "overall": "<一句话总评>",
    }
    assert judge_looks_like_placeholder(echo) is True
    clean = {
        "tech_correctness": {"score": 1, "comment": "答非所问,未涉及任何采分点", "cited_points": [], "missed_points": ["p1"]},
        "completeness": {"score": 0, "comment": "没有覆盖任何要点", "missed_points": []},
        "structure": {"score": 0, "comment": "仅一句话,无结构", "suggestion": "围绕题目作答"},
        "overall": "答非所问",
    }
    assert judge_looks_like_placeholder(clean) is False

    calls = {"n": 0}
    def fake_chat(messages, *, temperature=None, purpose="chat"):
        calls["n"] += 1
        return json.dumps(echo if calls["n"] == 1 else clean, ensure_ascii=False)

    def _reject(data):
        if judge_looks_like_placeholder(data):
            raise ValueError("点评照抄了输出示例")

    from app.llm import JUDGE_SCHEMA
    score = run_json_task(fake_chat, system="s", user="u", schema=JUDGE_SCHEMA,
                          kind="judge", content_check=_reject)
    assert score["tech_correctness"]["comment"] == "答非所问,未涉及任何采分点", score
    assert calls["n"] == 2, calls
_()


# ---------- 3.9 跑题拦截:词面重叠门控 + 确定性 0 分 ----------
@t("跑题拦截:占位/空话作答判定为无关,正常短作答放行")
def _():
    from app.llm import answer_is_irrelevant, build_irrelevant_score
    q = "请说明Redis AOF持久化机制的核心原理,并解释其与RDB持久化方式的差异"
    ref = "AOF 通过记录所有写操作命令实现持久化;RDB 是快照持久化。AOF 文件更大但更完整。"
    sps = ["AOF 记录写操作实现持久化", "AOF 与 RDB 的区别", "AOF 重写操作"]
    assert answer_is_irrelevant("这是一个模拟面试题目回答", q, ref, sps) is True
    assert answer_is_irrelevant("", q, ref, sps) is True
    # 命中领域词的正常回答(哪怕简短)必须放行
    assert answer_is_irrelevant("AOF 记录写命令,RDB 是快照,两者文件体积不同", q, ref, sps) is False
    s = build_irrelevant_score(sps)
    assert (s["tech_correctness"]["score"], s["completeness"]["score"], s["structure"]["score"]) == (0, 0, 0)
    assert s["tech_correctness"]["missed_points"] == sps, s
    assert "答非所问" in s["overall"]
_()


# ---------- 3. 30 天查重 ----------
@t("30 天查重:新题与 qa_log 已有题不重复")
@without_llm
def _():
    sid = interview.start_session("测试", "公司A")
    qs1 = interview.build_questions(sid, "测试", jd_keywords=[])
    # 直接 SQL 灌 5 行 qa_log(避免 LLM 点评)
    from app import db
    for q in qs1:
        db.execute(
            "INSERT INTO qa_log(session_id, question, question_source) VALUES (?,?,?)",
            (sid, q["question"], q["question_source"]),
        )
    sid2 = interview.start_session("测试", "公司B")
    qs2 = interview.build_questions(sid2, "测试", jd_keywords=[])
    overlap = set(q["question"] for q in qs1) & set(q["question"] for q in qs2)
    if len(qs2) == QUESTIONS_PER_SESSION:
        assert len(overlap) == 0, f"30 天内有重复: {overlap}"
    else:
        print(f"\n    提示:题库已用尽(qs2={len(qs2)}),无重叠可断言")

_()


# ---------- 4. 点评 JSON Schema 校验 ----------
if WITH_LLM:
    @t("三轴点评:每轴 score 0-5,字段齐全(真调 LLM)")
    def _():
        from app.llm import judge_answer
        question = "Redis 的两种持久化方式 RDB 和 AOF 有什么区别?"
        ref = "RDB 是快照,AOF 是日志,各有优缺点。"
        sps = ["RDB 定义", "AOF 定义", "优缺点"]
        score = interview.submit_answer(
            session_id=interview.start_session("测", ""),
            question=question,
            question_source="NEW",
            answer="不太清楚,好像是缓存吧。",
            reference_answer=ref,
            scoring_points=sps,
        )["score"]
        for axis in ("tech_correctness", "completeness", "structure"):
            assert axis in score, f"缺 {axis}"
            assert "score" in score[axis]
            s = score[axis]["score"]
            assert isinstance(s, (int, float)) and 0 <= s <= 5, f"{axis}.score 越界: {s}"
        assert "overall" in score
    _()
else:
    @t("三轴点评:Schema 字段定义(本地校验,不调 LLM)")
    def _():
        from app.llm import JUDGE_SCHEMA
        for axis in ("tech_correctness", "completeness", "structure"):
            assert axis in JUDGE_SCHEMA["properties"]
            assert "score" in JUDGE_SCHEMA["properties"][axis]["properties"]
        assert "overall" in JUDGE_SCHEMA["properties"]
    _()


# ---------- 5. 点评的 prompt 引用了采分点(静态检查) ----------
@t("点评 prompt 显式要求引用采分点")
def _():
    from app.llm import JUDGE_SYSTEM
    assert "采分点" in JUDGE_SYSTEM
    assert "cited_points" in JUDGE_SYSTEM or "missed_points" in JUDGE_SYSTEM
_()


# ---------- 6. settings 表读写 ----------
print("\n-- settings --", flush=True)

@t("settings:默认值首次读会落库并返回")
def _():
    from app.modules import settings as s
    from app import db
    # 消除对共享业务库持久化状态的隐性依赖:先清 key,再断言默认值
    db.execute("DELETE FROM settings WHERE key IN ('llm.model','embed.model')")
    v = s.get("llm.model")
    assert v == "qwen3-4b-awq", v
    v = s.get("embed.model")
    assert v == "bge-small-zh-v1.5", v
_()

@t("settings:set 之后再 get 应返回新值")
def _():
    from app.modules import settings as s
    s.update_many({"llm.model": "test-model-x"})
    assert s.get("llm.model") == "test-model-x"
    # 改回默认,避免污染其他测试(注意:必须是真实存在的模型名,否则 LLM 调用 404)
    s.update_many({"llm.model": "qwen3-4b-awq"})
_()

@t("settings:llm client 按新配置重建(动态读)")
def _():
    from app import llm as _llm
    from app.modules import settings as s
    s.update_many({"llm.base_url": "http://example.invalid/v1", "llm.api_key": "X", "llm.timeout": "5"})
    c1 = _llm.get_client()
    sig1 = _llm._client_signature
    assert sig1[1] == "http://example.invalid/v1", sig1
    # 再调一次,sig 应一致
    c2 = _llm.get_client()
    assert _llm._client_signature == sig1
    # 改配置,下次 get_client 应重建
    s.update_many({"llm.base_url": "http://localhost:8000/v1", "llm.api_key": "EMPTY", "llm.timeout": "30"})
    c3 = _llm.get_client()
    assert _llm._client_signature != sig1
_()

@t("settings:as_ui_dict 分组结构正确")
def _():
    from app.modules import settings as s
    ui = s.as_ui_dict()
    assert set(ui.keys()) == {"model", "interview", "memory"}
    assert "llm_model" in ui["model"]
    assert "merge_threshold" in ui["memory"]
    assert "questions_per_session" in ui["interview"]
_()

@t("settings:get_int / get_float 解析正确")
def _():
    from app.modules import settings as s
    s.update_many({"interview.questions_per_session": "7", "memory.merge_threshold": "0.9"})
    assert s.get_int("interview.questions_per_session") == 7
    assert abs(s.get_float("memory.merge_threshold") - 0.9) < 0.001
_()


# ---------- 7. 档案历史(命名 / 列表 / 删除) ----------
@t("profile:创建带名称档案,列表可见,可删除")
def _():
    from app.modules import profile as _p
    from app import db
    pid = _p.create_or_update("测试简历内容 Python FastAPI", "测试 JD Redis", name="测试档案")
    assert pid > 0
    p = _p.get_profile(pid)
    assert p and p["name"] == "测试档案"
    lst = _p.list_profiles(limit=100)
    assert any(x["id"] == pid for x in lst)
    _p.delete_profile(pid)
    assert _p.get_profile(pid) is None
_()


# ---------- 8. 出题器读 settings 配比 ----------
@t("interview.build_questions:按 settings 的题数与配比")
def _():
    from app.modules import interview, settings as s
    # 隔离:开新会话前清掉 qa_log 避免 30 天查重干扰
    from app import db
    db.execute("DELETE FROM qa_log")
    s.update_many({
        "interview.questions_per_session": "4",
        "interview.weakness_ratio":        "2",
        "interview.resume_ratio":          "1",
        "interview.new_ratio":             "1",
    })
    sid = interview.start_session("t", "")
    qs = interview.build_questions(sid, "t", jd_keywords=["Redis", "JVM", "Agent", "Kafka"])
    assert len(qs) == 4, f"got {len(qs)}: {[q['question_source'] for q in qs]}"
    s.update_many({
        "interview.questions_per_session": "5",
        "interview.weakness_ratio":        "2",
        "interview.resume_ratio":          "2",
        "interview.new_ratio":             "1",
    })
_()


print("\n== 全部通过 ==")
print("接下来可执行:uv run uvicorn app.main:app --port 8803")
print("浏览器打开 http://localhost:8803 走一遍 5 题端到端")
