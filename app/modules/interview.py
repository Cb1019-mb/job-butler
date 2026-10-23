"""interview 模块:出题器 + 作答 + 三轴点评 + qa_log 落库。

W1 范围(对齐文档 §二.1):
- 出题器:5 题配比 = 弱点 2 + 简历 2 + 岗位新知 1
- W1 弱点池为空,前 N 场会退化为"用其他类别填空",不影响链路跑通
- 30 天查重(FTS5 命中)
- 三轴流式点评
- W2 在此加记忆引擎召回
"""
from __future__ import annotations

import json
import logging
import random
import threading
from typing import Any

from .. import db
from ..llm import answer_is_irrelevant, build_irrelevant_score, generate_resume_questions, generate_session_summary, generate_targeted_questions, judge_answer, judge_answer_stream
from . import profile, question_bank, settings as _settings

log = logging.getLogger(__name__)

# 记忆抽取后台线程的串行锁(单用户工具,全局一把锁足够)
_extract_lock = threading.Lock()


# ---------- 出题器 ----------

def start_session(role_tag: str, company: str = "", profile_id: int | None = None) -> int:
    """新建会话(可绑定所选档案),返回 session_id。"""
    sid = db.execute(
        "INSERT INTO interview_session(role_tag, company, mode, profile_id) VALUES (?,?,?,?)",
        (role_tag, company, "MOCK", profile_id),
    )
    return sid


def _recent_question_ids(days: int | None = None) -> set[str]:
    """近 N 天已问过的题:qa_log(已作答)+ session_question(已出题未作答)。

    后者很关键:连续两场开面试时,第一场刚出的题还没作答,
    若只查 qa_log 第二场会原样重复。
    """
    if days is None:
        days = _settings.get_int("interview.dedup_days")
    win = (f"-{days} days",)
    rows = db.fetch_all(
        """
        SELECT question FROM qa_log
        WHERE datetime(created_at) >= datetime('now', ?)
        """,
        win,
    )
    qs = {r["question"] for r in rows}
    rows2 = db.fetch_all(
        """
        SELECT DISTINCT sq.question AS question
        FROM session_question sq
        JOIN interview_session s ON sq.session_id = s.id
        WHERE datetime(s.created_at) >= datetime('now', ?)
        """,
        win,
    )
    qs |= {r["question"] for r in rows2}
    return qs


def _llm_slot_questions(topics: list[str], needed: int, exclude: set[str], context: str) -> list[dict[str, Any]]:
    """通用 LLM 定向出题:按 topics 现场生成,过滤已问题面。返回不超过 needed 条。"""
    if needed <= 0 or not topics:
        return []
    out: list[dict[str, Any]] = []
    try:
        qs = generate_targeted_questions(topics, n=needed, avoid=list(exclude)[:12], context=context)
    except Exception as e:
        log.warning("LLM 定向出题失败: %s", e)
        return out
    for q in qs:
        qtxt = (q.get("question") or "").strip()
        if not qtxt or qtxt in exclude or any(x["question"] == qtxt for x in out):
            continue
        out.append({
            "id": f"gen-{len(out)}",
            "category": q.get("category") or "",
            "difficulty": 3,
            "jd_keywords": [],
            "question": qtxt,
            "reference_answer": q.get("reference_answer", ""),
            "scoring_points": q.get("scoring_points", []),
        })
        if len(out) >= needed:
            break
    return out


def _profile_extra_topics(profile_id: int | None, keys: tuple[str, ...] = ("weak_signals", "jd_focus")) -> list[str]:
    """从所选档案取额外考察目标(用于 LLM 补题)。"""
    skills = profile.get_parsed_skills(profile_id)
    if not isinstance(skills, dict):
        return []
    topics: list[str] = []
    for k in keys:
        topics += [str(t) for t in (skills.get(k) or []) if str(t).strip()]
    # 去重保序
    return list(dict.fromkeys(topics))


def _pick_weakness_questions(n: int, exclude: set[str], session_id: int = 0, profile_id: int | None = None) -> list[dict[str, Any]]:
    """弱点再训练槽:LLM 按薄弱知识点现场出题(每场不同),题库只做失败兜底。

    - memory.enabled 开:优先用认知记忆里的 ACTIVE 弱点作为考察目标
    - 无记忆:退化到所选档案的 weak_signals(疑似短板)
    - memory.enabled 关(A/B 对照):不查记忆不调 LLM,直接走题库兜底
    """
    from . import memory_engine
    if not _settings.get_bool("memory.enabled"):
        pool = question_bank.by_category("项目深挖") + question_bank.by_category("HR")
        pool = [q for q in pool if q["question"] not in exclude]
        return pool[:n]

    weaknesses = memory_engine.search_active_weaknesses(k=max(n * 6, 12))
    # 按 session_id 偏移轮换,不同场次覆盖不同弱点
    if weaknesses and session_id > 0:
        offset = session_id % max(len(weaknesses), 1)
        weaknesses = weaknesses[offset:] + weaknesses[:offset]

    if weaknesses:
        topics = [(w.get("content") or "").strip() for w in weaknesses[:n]]
        topics = [t for t in topics if t]
        context = "历史面试暴露的弱点(认知记忆),考察候选人是否已补齐"
    else:
        topics = _profile_extra_topics(profile_id, keys=("weak_signals",))[:n]
        context = "候选人能力档案中的疑似短板"

    out = _llm_slot_questions(topics, n, exclude, context)
    from .memory_engine import _guess_knowledge
    default_cat = _guess_knowledge(topics[0]) if topics else "弱点再训练"
    for q in out:
        q["category"] = q["category"] or default_cat or "弱点再训练"

    # LLM 产出不足(目标被合并/失败)时,换档案里的其他短板/JD 重点补题
    if len(out) < n:
        exclude |= {q["question"] for q in out}
        extra = [t for t in _profile_extra_topics(profile_id) if t not in topics][:6]
        if not extra and weaknesses:
            extra = [(w.get("content") or "").strip() for w in weaknesses[n:n + 4] if (w.get("content") or "").strip()]
        more = _llm_slot_questions(extra, n - len(out), exclude, context + "(换角度补题)")
        for q in more:
            q["category"] = q["category"] or default_cat or "弱点再训练"
        out += more

    # 最终兜底:题库补齐
    if len(out) < n:
        exclude |= {q["question"] for q in out}
        if weaknesses:
            from .memory_engine import KNOWLEDGE_KEYWORDS
            pool = question_bank.load_bank()
            for w in weaknesses:
                if len(out) >= n:
                    break
                content = (w.get("content") or "").strip()
                tech_kws = {kw for kw in KNOWLEDGE_KEYWORDS if kw and kw in content} or {content[:6]}
                best_q, best_score = None, 0
                for q in pool:
                    if q["question"] in exclude:
                        continue
                    qcat = q.get("category", "")
                    score = (
                        sum(1 for kw in tech_kws if kw in q["question"]) +
                        sum(2 for kw in tech_kws if kw in qcat) +
                        sum(1 for jk in q.get("jd_keywords", []) if jk and jk in content)
                    )
                    if score > best_score:
                        best_score, best_q = score, q
                if best_q is not None and best_score > 0:
                    out.append({**best_q})
                    exclude.add(best_q["question"])
        pool = question_bank.by_category("项目深挖") + question_bank.by_category("HR")
        for fb in pool:
            if len(out) >= n:
                break
            if fb["question"] in exclude:
                continue
            out.append({**fb})
            exclude.add(fb["question"])
    return out[:n]


def _pick_resume_questions(n: int, exclude: set[str], profile_id: int | None = None) -> list[dict[str, Any]]:
    """简历深挖槽:每个项目 LLM 现出 1 题;项目数不足 n 或失败时,按项目技术栈换角度 LLM 补题;最后题库兜底。

    exclude(近 N 天已答题面)会作为"避免重复"提示传给出题 prompt,保证不同场次题目不撞车。
    """
    skills = profile.get_parsed_skills(profile_id)
    projects = skills.get("projects", []) if isinstance(skills, dict) else []
    questions: list[dict[str, Any]] = []
    avoid = list(exclude)[:12]
    for p in projects[:n]:
        try:
            qs = generate_resume_questions(p, n=1, avoid=avoid)
            for q in qs:
                qtxt = q.get("question") or ""
                if not qtxt or qtxt in exclude or any(x["question"] == qtxt for x in questions):
                    continue
                questions.append(
                    {
                        "id": f"gen-{p.get('name','')}-{len(questions)}",
                        "category": p.get("name", "项目深挖"),
                        "difficulty": 2,
                        "jd_keywords": p.get("tech", []),
                        "question": qtxt,
                        "reference_answer": q["reference_answer"],
                        "scoring_points": q["scoring_points"],
                    }
                )
                if len(questions) >= n:
                    break
        except Exception as e:
            log.warning("generate_resume_questions failed: %s", e)
        if len(questions) >= n:
            break
    # 项目少 / LLM 失败:用项目技术栈与项目名做目标,换角度补题
    if len(questions) < n and projects:
        topics: list[str] = []
        for p in projects:
            if p.get("name"):
                topics.append(p["name"])
            topics += [str(t) for t in (p.get("tech") or []) if str(t).strip()]
        topics = list(dict.fromkeys(topics))[:6] or ["简历中的项目经历"]
        more = _llm_slot_questions(topics, n - len(questions), exclude | {q["question"] for q in questions},
                                   context="从简历项目出发的深挖题(换角度,不出语义相同的题)")
        for q in more:
            q["category"] = q["category"] or "项目深挖"
        questions += more
    # 最终兜底:题库
    if len(questions) < n:
        for fb in question_bank.by_category("项目深挖"):
            if len(questions) >= n:
                break
            if fb["question"] not in exclude and fb not in questions:
                questions.append(fb)
    return questions[:n]


def _pick_new_questions(n: int, jd_keywords: list[str], exclude: set[str], profile_id: int | None = None) -> list[dict[str, Any]]:
    """岗位新知槽:JD 关键词 LLM 现场出题(带避重);不足时题库随机抽样,再不足用所选档案 jd_focus 补题。"""
    out = _llm_slot_questions(jd_keywords[:5], n, exclude,
                              context="目标岗位 JD 重点能力(岗位新知考察)")
    for q in out:
        q["category"] = q["category"] or "岗位新知"
        q["jd_keywords"] = jd_keywords
    # 题库随机抽样兜底
    if len(out) < n:
        exclude |= {q["question"] for q in out}
        qs = question_bank.search_by_jd_keywords(jd_keywords, limit=20) if jd_keywords else question_bank.load_bank()
        qs = [q for q in qs if q["question"] not in exclude]
        if not qs:
            qs = [q for q in question_bank.load_bank() if q["question"] not in exclude]
        if len(qs) > (n - len(out)):
            qs = random.sample(qs, n - len(out))
        out += list(qs)
    # 题库也耗尽:用所选档案的 jd_focus 再补
    if len(out) < n:
        exclude |= {q["question"] for q in out}
        jd_focus = _profile_extra_topics(profile_id, keys=("jd_focus",))[:5]
        more = _llm_slot_questions(jd_focus or ["后端工程师岗位的通用新知识"],
                                   n - len(out), exclude, context="岗位新知考察(换角度补题)")
        for q in more:
            q["category"] = q["category"] or "岗位新知"
        out += more
    return out[:n]


def build_questions(session_id: int, role_tag: str, jd_keywords: list[str] | None = None, profile_id: int | None = None) -> list[dict[str, Any]]:
    """出 N 题(N 与配比读自 settings;题目来源可绑定指定档案 profile_id)。

    三个槽位并行生成(单槽 10~30s,串行一场要 1 分钟+,并行压到最慢单槽的耗时);
    槽位间通过"已用"集合增量去重;最终不足 n_total 时 LLM 换角度补题,保证题数恒定。
    """
    jd_keywords = jd_keywords or []
    n_total = _settings.get_int("interview.questions_per_session")
    n_weak = _settings.get_int("interview.weakness_ratio")
    n_resume = _settings.get_int("interview.resume_ratio")
    n_new = _settings.get_int("interview.new_ratio")
    # 30 天查重:recent 共享(避免同一场内出重复)
    # 但**不跨槽位共享 used**:WEAKNESS 和 RESUME 兜底都从同一题库"项目深挖"分类抽,
    # 跨槽共享 used 会导致 RESUME 全部被 used 过滤掉(只 WEAK 占项目深挖题)。
    recent = _recent_question_ids()
    recent_exclude: set[str] = set(recent)
    out: list[dict[str, Any]] = []
    used_per_source: dict[str, set[str]] = {}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="qgen") as pool:
        # 每槽**多产几道**(配额 × 2)以防跨槽撞题后无替代
        f_weak = pool.submit(_pick_weakness_questions, n_weak * 2, set(recent_exclude), session_id=session_id, profile_id=profile_id)
        f_resume = pool.submit(_pick_resume_questions, n_resume * 2, set(recent_exclude), profile_id=profile_id)
        f_new = pool.submit(_pick_new_questions, n_new * 2, list(jd_keywords), set(recent_exclude), profile_id=profile_id)
        results = [("WEAKNESS", f_weak.result()), ("RESUME", f_resume.result()), ("NEW", f_new.result())]

    # 跨槽去重合并:每槽配额内,先 WEAK 再 RESUME 再 NEW(优先级),
    # 撞题就跳过(用该槽的后续候选,没有就缺口)
    seen: set[str] = set(recent_exclude)
    out: list[dict[str, Any]] = []
    by_source: dict[str, list[dict[str, Any]]] = {src: qs for src, qs in results}
    priority = [("WEAKNESS", n_weak), ("RESUME", n_resume), ("NEW", n_new)]
    for src, quota in priority:
        taken = 0
        for q in by_source.get(src, []):
            if taken >= quota:
                break
            if q["question"] in seen:
                continue
            out.append({**q, "question_source": src})
            seen.add(q["question"])
            taken += 1
    # 任何槽位未达配额,从该槽剩余候选补(不再跨槽借调,避免误标 source)
    if len(out) < n_total:
        for src, quota in priority:
            have = sum(1 for x in out if x["question_source"] == src)
            if have >= quota:
                continue
            for q in by_source.get(src, []):
                if have >= quota:
                    break
                if q["question"] in seen:
                    continue
                out.append({**q, "question_source": src})
                seen.add(q["question"])
                have += 1

    # 末尾兜底:槽位产出不足(LLM 合并目标 / 题库被 30 天窗口耗尽)时 LLM 换角度补齐
    if len(out) < n_total:
        topics = [t for t in _profile_extra_topics(profile_id)][:5] or [
            f"{role_tag}岗位的高频面试考点", "系统设计与工程实践", "技术深挖与原理辨析"]
        fill = _llm_slot_questions(topics, n_total - len(out), seen,
                                   context="补题:换角度出新题,不要与近期已考题语义相同")
        for q in fill:
            if q["question"] in seen:
                continue
            out.append({**q, "question_source": "NEW"})
            seen.add(q["question"])

    # 硬兜底:LLM 补题也失败时,放宽 30 天查重(仅对本场去重)从题库补满,保证题数恒定
    if len(out) < n_total:
        # 拷贝后再洗牌:load_bank 是 lru_cache 共享列表,原地 shuffle 会污染全局缓存且并发不安全
        bank = question_bank.load_bank()
        pool = random.sample(bank, k=len(bank))
        for q in pool:
            if len(out) >= n_total:
                break
            if q["question"] in seen:
                continue
            out.append({**q, "question_source": "NEW"})
            seen.add(q["question"])

    return out[:n_total]


# ---------- 作答 + 点评 + 落库 ----------

def submit_answer(
    session_id: int,
    question: str,
    question_source: str,
    answer: str,
    reference_answer: str,
    scoring_points: list[str],
) -> dict[str, Any]:
    """同步版:一次性拿三轴评分,落库,返回 score 字典。"""
    if answer_is_irrelevant(answer, question, reference_answer, scoring_points):
        # 跑题/空话不过 LLM:4B 模型会把它幻觉成高分点评
        score = build_irrelevant_score(scoring_points)
    else:
        score = judge_answer(
            question=question,
            reference_answer=reference_answer,
            scoring_points=scoring_points,
            answer=answer,
        )
    qa_id = db.execute(
        """
        INSERT INTO qa_log(session_id, question, question_source, answer, score_json, reference_answer)
        VALUES (?,?,?,?,?,?)
        """,
        (session_id, question, question_source, answer, json.dumps(score, ensure_ascii=False), reference_answer),
    )
    # W2:自动跑记忆抽取(规则兜底,LLM 失败不阻塞);idempotent
    try:
        from . import memory_engine
        memory_engine.extract_and_store_for_session(session_id)
    except Exception as e:
        log.warning("extract_and_store_for_session failed: %s", e)
    return {"qa_id": qa_id, "score": score}


def _bg_extract(session_id: int) -> None:
    """后台线程:跑记忆抽取(不阻塞 SSE 响应)。幂等,串行锁防并发重复。"""
    def _run():
        try:
            with _extract_lock:
                from . import memory_engine
                memory_engine.extract_and_store_for_session(session_id)
        except Exception as e:
            log.warning("bg extract failed for session %s: %s", session_id, e)
    threading.Thread(target=_run, daemon=True, name=f"extract-{session_id}").start()


def submit_answer_stream(
    session_id: int,
    question: str,
    question_source: str,
    answer: str,
    reference_answer: str,
    scoring_points: list[str],
):
    """流式版 generator:逐 chunk yield {"type":"chunk"},落库后立即 yield {"type":"done"}。

    done 事件在点评落库后马上发出;记忆抽取移入后台线程执行。
    修复:原先 done 要等整场记忆抽取(每题一次 LLM,10~30s)才发出,
    前端按钮长时间停在"点评中",看起来像无响应。
    """
    if answer_is_irrelevant(answer, question, reference_answer, scoring_points):
        # 与同步版对齐:跑题/空话不过 LLM
        score = build_irrelevant_score(scoring_points)
        qa_id = db.execute(
            """
            INSERT INTO qa_log(session_id, question, question_source, answer, score_json, reference_answer)
            VALUES (?,?,?,?,?,?)
            """,
            (session_id, question, question_source, answer, json.dumps(score, ensure_ascii=False), reference_answer),
        )
        yield {"type": "done", "id": qa_id, "score": score}
        _bg_extract(session_id)
        return
    scoring_text = "\n".join(f"- {p}" for p in scoring_points)
    buf: list[str] = []
    for chunk in judge_answer_stream(question, reference_answer, scoring_text, answer):
        buf.append(chunk)
        yield {"type": "chunk", "text": chunk}
    full = "".join(buf)
    # 流式路径同样要做键名容错/Schema 归一化(与同步版 judge_answer 一致):
    # 模型可能返回扁平数字分 {"tech_correctness": 3},不归一会让后续读取崩溃
    from ..json_utils import ensure_judge_citations, extract_json, judge_looks_like_placeholder, normalize_keys, validate_schema
    from ..llm import JUDGE_SCHEMA
    score: dict[str, Any] = {"raw": full}
    try:
        score = normalize_keys(extract_json(full), kind="judge")
        validate_schema(score, JUDGE_SCHEMA)
    except Exception:
        import re
        m = re.search(r"\{[\s\S]*\}", full)
        if m:
            try:
                score = normalize_keys(json.loads(m.group(0)), kind="judge")
            except Exception:
                pass
    # 与同步版 judge_answer 容错对齐:模型空引用时用采分点补 missed_points,保证点评有依据
    score = ensure_judge_citations(score, scoring_points)
    # 流式无法中途重试:检测到照抄示例的点评,改用同步版重评一次再落库(done 事件携带重评结果,
    # 前端用它替换原始流式内容展示)
    if judge_looks_like_placeholder(score):
        log.warning("stream judge echoed the prompt example, re-judging synchronously")
        try:
            score = judge_answer(
                question=question, reference_answer=reference_answer,
                scoring_points=scoring_points, answer=answer,
            )
        except Exception as e:
            log.warning("stream judge re-ask failed: %s", e)
    qa_id = db.execute(
        """
        INSERT INTO qa_log(session_id, question, question_source, answer, score_json, reference_answer)
        VALUES (?,?,?,?,?,?)
        """,
        (session_id, question, question_source, answer, json.dumps(score, ensure_ascii=False), reference_answer),
    )
    # done 立即返回,并携带归一化后的 score,前端据此渲染格式化点评(替代原始 JSON 流)
    yield {"type": "done", "id": qa_id, "score": score}
    # 记忆抽取后台执行(幂等保护,不会重复入库)
    _bg_extract(session_id)


# ---------- 题目持久化(W4 修复 #9) ----------

def persist_questions(session_id: int, questions: list[dict[str, Any]]) -> None:
    """把会话题目落到 session_question 表(可持久化,重启不丢)。"""
    db.execute("DELETE FROM session_question WHERE session_id=?", (session_id,))
    for i, q in enumerate(questions):
        db.execute(
            """
            INSERT INTO session_question(session_id, qa_index, question, question_source, category, reference_answer, scoring_points_json)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                session_id,
                i,
                q.get("question", ""),
                q.get("question_source", ""),
                q.get("category", ""),
                q.get("reference_answer", ""),
                json.dumps(q.get("scoring_points") or [], ensure_ascii=False),
            ),
        )


def load_questions(session_id: int) -> list[dict[str, Any]]:
    """从 session_question 表恢复题目;为空则返回空列表。"""
    rows = db.fetch_all(
        "SELECT qa_index, question, question_source, category, reference_answer, scoring_points_json "
        "FROM session_question WHERE session_id=? ORDER BY qa_index",
        (session_id,),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            sps = json.loads(r.get("scoring_points_json") or "[]")
        except Exception:
            sps = []
        out.append({
            "question": r["question"],
            "question_source": r["question_source"],
            "category": r.get("category", ""),
            "reference_answer": r.get("reference_answer", ""),
            "scoring_points": sps,
        })
    return out


def get_qa_log(qa_id: int) -> dict[str, Any] | None:
    """W4 修复 #1:按 qa_id 单条查询(供 timeline "查看原题"按钮)。"""
    return db.fetch_one("SELECT * FROM qa_log WHERE id=?", (qa_id,))


def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    return db.fetch_all("SELECT * FROM interview_session ORDER BY id DESC LIMIT ?", (limit,))


def delete_session(session_id: int) -> None:
    """删除会话(qa_log / session_question 经 FK CASCADE 一并删除)。"""
    db.execute("DELETE FROM interview_session WHERE id=?", (session_id,))


# ---------- 会话综合点评 ----------

def summarize_session(session_id: int) -> dict[str, Any]:
    """整场综合点评:先确保记忆抽取完成(幂等),再聚合全部题目评分交给 LLM 复盘。

    返回 {summary_md, stats};结果持久化到 interview_session.summary_md。
    """
    rows = get_qa_logs(session_id)
    answered = [r for r in rows if (r.get("answer") or "").strip()]
    if not answered:
        raise ValueError("本场还没有作答记录,请先作答")

    # 后台抽取可能尚未跑完;非阻塞抢锁:抢到就补一次(幂等),抢不到说明后台正在抽,不等待
    if _extract_lock.acquire(blocking=False):
        try:
            from . import memory_engine
            memory_engine.extract_and_store_for_session(session_id)
        except Exception as e:
            log.warning("summarize 前补抽取失败: %s", e)
        finally:
            _extract_lock.release()

    qa_items: list[dict[str, Any]] = []
    axis_scores: dict[str, list[float]] = {"tech_correctness": [], "completeness": [], "structure": []}
    for r in answered:
        try:
            score = json.loads(r.get("score_json") or "{}")
        except Exception:
            score = {}
        for axis in axis_scores:
            sub = score.get(axis)
            v = sub.get("score") if isinstance(sub, dict) else sub
            if isinstance(v, (int, float)):
                axis_scores[axis].append(float(v))
        qa_items.append({"question": r["question"], "answer": r.get("answer", ""), "score": score})

    summary_md = generate_session_summary(qa_items)
    db.execute("UPDATE interview_session SET summary_md=? WHERE id=?", (summary_md, session_id))

    n_scored = len(axis_scores["tech_correctness"])
    stats = {
        "answered": len(answered),
        "scored": n_scored,
        "avg": {
            axis: (round(sum(v) / len(v), 2) if v else None)
            for axis, v in axis_scores.items()
        },
    }
    return {"summary_md": summary_md, "stats": stats}


def get_qa_logs(session_id: int) -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT id, question, question_source, answer, score_json, reference_answer, extracted_at, created_at "
        "FROM qa_log WHERE session_id=? ORDER BY id",
        (session_id,),
    )
