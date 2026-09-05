"""记忆引擎:W2 完整实现。

流水线:抽取(规则 + LLM)→ 质量门 → 合并(similarity > threshold) → 衰减 → 状态机 → 场次上限。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import re
from typing import Any

import numpy as np

from .. import db
from ..embed import (
    blob_to_embedding,
    encode_doc,
    embedding_to_blob,
)
from ..json_utils import extract_json, normalize_keys
from .. import llm as _llm
from . import settings as _settings

log = logging.getLogger(__name__)

# ---------- 状态机 ----------
STATUS_ACTIVE = "ACTIVE"
STATUS_MASTERED = "MASTERED"
STATUS_IDLE = "IDLE"
STATUS_DISABLED = "DISABLED"

# ---------- 关键词质量门(规则兜底) ----------
KNOWLEDGE_KEYWORDS = {
    "Redis", "AOF", "RDB", "缓存", "持久化", "淘汰",
    "MySQL", "索引", "B+ 树", "事务", "MVCC", "InnoDB", "间隙锁",
    "Kafka", "消息队列", "分区", "副本", "ISR", "幂等",
    "JVM", "GC", "G1", "CMS", "类加载", "双亲委派",
    "Spring", "IoC", "AOP", "Bean", "事务传播",
    "Python", "GIL", "装饰器", "生成器", "协程",
    "操作系统", "进程", "线程", "虚拟内存", "LRU", "页面置换",
    "网络", "TCP", "HTTP", "HTTPS", "TLS", "三次握手", "四次挥手",
    "限流", "令牌桶", "漏桶", "缓存穿透", "雪崩", "击穿", "短链", "发号器",
    "Agent", "RAG", "MCP", "Function Call", "LoRA", "RLHF", "DPO",
    "HashMap", "红黑树", "链表", "DP", "BFS", "DFS", "Dijkstra",
    "CAP", "分布式", "高可用", "扩容",
    "项目", "STAR", "指标", "量化",
}

# 强信号短语(就算关键词匹配失败,有这些短语也算"具体")
SPECIFIC_PHRASES = [
    r"答错了", r"漏了点", r"缺了.{0,10}这点", r"没有提到",
    r"说反了", r"概念混淆", r"不清楚", r"不知道",
    r"答非所问", r"跑题", r"问的是.{0,15}你答的是",
]

# 弱信号短语(纯空话,直接 reject)
WEAK_PHRASES = [
    r"表达不够自信", r"显得紧张", r"语速太快", r"说话有点快",
    r"应该多练", r"需要加强", r"基础不太扎实", r"经验不足",
    r"再准备一下", r"继续努力", r"多看多学",
]


# ---------- 衰减 / 状态机 ----------

def _now_epoch_days() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp() / 86400.0


def _parse_db_time(s: str) -> _dt.datetime:
    """DB 的 datetime('now') 存的是 UTC naive 时间,统一按 UTC 解析,避免 8 小时偏差。"""
    return _dt.datetime.fromisoformat(s).replace(tzinfo=_dt.timezone.utc)


def _half_life(type_: str) -> float:
    key = {"WEAKNESS": "memory.half_life_weakness",
           "FACT":     "memory.half_life_fact",
           "PREF":     "memory.half_life_pref"}.get(type_, "memory.half_life_weakness")
    return _settings.get_float(key)


def effective_strength(memory_row: dict[str, Any], now_epoch_days: float | None = None) -> float:
    if now_epoch_days is None:
        now_epoch_days = _now_epoch_days()
    last = _parse_db_time(memory_row["last_reinforced_at"]).timestamp() / 86400.0
    days = max(0.0, now_epoch_days - last)
    hl = float(memory_row.get("half_life_days") or _half_life(memory_row.get("type", "WEAKNESS")))
    decay = math.exp(-math.log(2) * days / hl)
    return float(memory_row.get("strength", 1.0)) * decay


def refresh_idle_statuses() -> int:
    """把 effective < 0.1 或超过 idle_days 无强化的 ACTIVE 记忆标记为 IDLE。返回更新条数。"""
    idle_days = _settings.get_int("memory.idle_days")
    rows = db.fetch_all("SELECT * FROM memory WHERE status=?", (STATUS_ACTIVE,))
    n = 0
    now = _dt.datetime.now(_dt.timezone.utc)
    for r in rows:
        eff = effective_strength(r)
        last = _parse_db_time(r["last_reinforced_at"])
        days_idle = (now - last).days
        if eff < 0.1 or days_idle >= idle_days:
            update_status(r["id"], STATUS_IDLE)
            n += 1
    return n


# ---------- 抽取 ----------

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "weaknesses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "knowledge": {"type": "string", "description": "具体知识点(技术词)"},
                },
                "required": ["content", "knowledge"],
            },
            "description": "本场暴露的弱点,1~6 条,无则空数组",
        },
    },
    "required": ["weaknesses"],
}

EXTRACT_SYSTEM = """你是求职教练复盘员。从候选人的作答 + 三轴点评里抽取"具体知识点弱点"。
要求:
1. 每条 weakness 必须含明确知识点(技术名词 / 算法名 / 协议名 / 项目名)+ 可验证描述;
2. 不要"表达不够自信""基础不扎实"这类空话;
3. 没有就输出空数组;
4. 只输出严格符合 JSON Schema 的 JSON,不要解释或 markdown 包裹。"""


def _rule_extract(qa: dict[str, Any]) -> list[dict[str, str]]:
    """规则兜底:从漏点 + 答案长度粗略抽。"""
    out: list[dict[str, str]] = []
    score = qa.get("score") or {}
    completeness = score.get("completeness", {}) if isinstance(score, dict) else {}
    missed = completeness.get("missed_points") or []
    for m in missed:
        if not isinstance(m, str):
            continue
        m_strip = m.strip()
        if len(m_strip) < 3:
            continue
        if any(re.search(p, m_strip) for p in WEAK_PHRASES):
            continue
        out.append({"content": m_strip, "knowledge": _guess_knowledge(m_strip)})
    answer = (qa.get("answer") or "").strip()
    if len(answer) < 15 and answer:
        tech_kw = _guess_knowledge((qa.get("question") or "") + " " + " ".join(missed))
        if tech_kw and tech_kw != "作答完整性":
            out.append({
                "content": f"对 {tech_kw} 回答过短({len(answer)} 字),明显未掌握",
                "knowledge": tech_kw,
            })
        else:
            out.append({
                "content": f"回答过短({len(answer)} 字):{answer[:30]}",
                "knowledge": "作答完整性",
            })
    return out


def _guess_knowledge(text: str) -> str:
    """从文本中找知识点关键词:取命中列表里最长的一个(更具体的词优先,
    如 'Redis AOF 持久化机制答错' 应归为 Redis 而非 持久化)。"""
    hits = [kw for kw in KNOWLEDGE_KEYWORDS if kw and kw in text]
    if not hits:
        return ""
    return max(hits, key=len)


def _quality_pass(item: dict[str, str], context: str = "") -> bool:
    """质量门:有具体知识点 + 非空话 + 长度足够。

    context:题面/漏点等额外上下文,用于弥补"漏点本身没关键词但题目有"的情况。
    """
    content = (item.get("content") or "").strip()
    knowledge = (item.get("knowledge") or "").strip()
    if len(content) < 3:
        return False
    if any(re.search(p, content) for p in WEAK_PHRASES):
        return False
    has_kw = bool(knowledge) and knowledge != "作答完整性"
    has_specific = any(re.search(p, content) for p in SPECIFIC_PHRASES)
    if has_kw or has_specific:
        return True
    # 兜底:看 context 里有没有技术关键词
    if context and any(kw and kw in context for kw in KNOWLEDGE_KEYWORDS):
        return True
    return False


def _llm_extract(qa: dict[str, Any]) -> list[dict[str, str]]:
    """LLM 抽取:把 question/answer/score 喂进去,要求 JSON 数组。"""
    score = qa.get("score") or {}
    user = (
        f"【题目】{qa.get('question','')}\n\n"
        f"【候选人作答】{qa.get('answer','')}\n\n"
        f"【三轴点评】\n{json.dumps(score, ensure_ascii=False, indent=2)}\n\n"
        "请按 Schema 抽取本场暴露的弱点(无则空数组)。"
    )
    try:
        text = _llm._chat(
            [
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": user},
            ],
            purpose="memory_extract",
        )
        data = extract_json(text)
        # 宽容:模型可能返回顶层数组,或 {"weaknesses": [...]},或单条
        if isinstance(data, list):
            weaknesses = data
        elif isinstance(data, dict):
            weaknesses = data.get("weaknesses") or data.get("weakness") or []
        else:
            weaknesses = []
        return [w for w in weaknesses if isinstance(w, dict)]
    except Exception as e:
        log.warning("LLM extract failed: %s", e)
        return []


def _dedup_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for it in items:
        k = it.get("content", "").strip()
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def extract_from_qa(qa: dict[str, Any]) -> list[dict[str, str]]:
    """单题抽取:规则 + LLM 合并去重,过质量门。"""
    rule_items = _rule_extract(qa)
    llm_items = _llm_extract(qa)
    merged = _dedup_items(rule_items + llm_items)
    # context:题面 + 漏点,供质量门兜底
    score = qa.get("score") or {}
    missed = (score.get("completeness") or {}).get("missed_points") or []
    context = (qa.get("question") or "") + " " + " ".join(str(m) for m in missed)
    return [it for it in merged if _quality_pass(it, context=context)]


# ---------- 合并 ----------

def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _try_merge(content: str) -> int | None:
    """与已有 ACTIVE/IDLE 记忆算 embedding 余弦,> 阈值则合并到强度更高者;返回被合并到的 memory_id 或 None。

    embedding 后端不可用时降级为 None(不合并,直接入库)。
    """
    try:
        # 记忆 vs 记忆是 doc-doc 相似度,与入库时的 encode_doc 同口径(不加 BGE 查询前缀)
        new_vec = encode_doc(content)
    except Exception as e:
        log.warning("encode_doc failed, skip merge: %s", e)
        return None
    threshold = _settings.get_float("memory.merge_threshold")
    candidates = db.fetch_all(
        "SELECT id, content, strength, embedding FROM memory WHERE status IN (?,?)",
        (STATUS_ACTIVE, STATUS_IDLE),
    )
    best_id: int | None = None
    best_sim: float = 0.0
    for c in candidates:
        if not c.get("embedding"):
            continue
        try:
            existing = blob_to_embedding(c["embedding"])
            sim = _cosine(new_vec, existing)
        except Exception as e:
            log.warning("blob_to_embedding failed, skip: %s", e)
            continue
        if sim > threshold and sim > best_sim:
            best_sim = sim
            best_id = c["id"]
    if best_id is None:
        return None
    db.execute(
        """
        UPDATE memory
        SET strength = strength + 1,
            reinforce_count = reinforce_count + 1,
            last_reinforced_at = datetime('now')
        WHERE id=?
        """,
        (best_id,),
    )
    return best_id


def add_memory(
    type_: str,
    content: str,
    source_qa_id: int | None = None,
    embedding_blob: bytes | None = None,
    half_life_days: float | None = None,
) -> int:
    """插入新记忆(WEAKNESS 类型会先尝试合并)。"""
    if embedding_blob is None:
        try:
            embedding_blob = embedding_to_blob(encode_doc(content))
        except Exception as e:
            log.warning("encode_doc failed, store without embedding: %s", e)
            embedding_blob = None
    if half_life_days is None:
        half_life_days = _half_life(type_)
    # 只有 WEAKNESS 类型参与合并(FACT/PREF 是事实/偏好,不该和"答错的知识点"合并)
    if type_ == "WEAKNESS":
        merged_into = _try_merge(content)
        if merged_into is not None:
            return merged_into
    return db.execute(
        """
        INSERT INTO memory(type, content, source_qa_id, embedding, half_life_days)
        VALUES (?,?,?,?,?)
        """,
        (type_, content, source_qa_id, embedding_blob, half_life_days),
    )


# ---------- 场次上限 ----------

def enforce_session_cap(qa_ids: list[int]) -> int:
    """单场面试后,如果 ACTIVE 记忆数 > max_per_session,裁剪到强度高者(其余置 IDLE)。返回裁剪条数。"""
    cap = _settings.get_int("memory.max_per_session")
    if not qa_ids:
        return 0
    placeholders = ",".join("?" * len(qa_ids))
    rows = db.fetch_all(
        f"SELECT * FROM memory WHERE status=? AND source_qa_id IN ({placeholders}) "
        f"ORDER BY last_reinforced_at DESC",
        (STATUS_ACTIVE, *qa_ids),
    )
    if len(rows) <= cap:
        return 0
    scored = [(r["id"], effective_strength(r)) for r in rows]
    scored.sort(key=lambda x: x[1], reverse=True)
    keep_ids = {sid for sid, _ in scored[:cap]}
    n = 0
    for r in rows:
        if r["id"] not in keep_ids:
            update_status(r["id"], STATUS_IDLE)
            n += 1
    return n


# ---------- 状态机操作 ----------

def update_status(memory_id: int, status: str) -> None:
    db.execute("UPDATE memory SET status=? WHERE id=?", (status, memory_id))


def mark_invalid(memory_id: int) -> None:
    update_status(memory_id, STATUS_DISABLED)


def mark_mastered(memory_id: int) -> None:
    update_status(memory_id, STATUS_MASTERED)


def mark_active(memory_id: int) -> None:
    update_status(memory_id, STATUS_ACTIVE)


def reinforce(memory_id: int) -> None:
    db.execute(
        """
        UPDATE memory
        SET strength = strength + 1,
            reinforce_count = reinforce_count + 1,
            last_reinforced_at = datetime('now')
        WHERE id=?
        """,
        (memory_id,),
    )


# ---------- 列表 / 检索 ----------

def list_memories(status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM memory"
    params: tuple = ()
    if status:
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY id DESC"
    return db.fetch_all(sql, params)


def get_memory(memory_id: int) -> dict[str, Any] | None:
    return db.fetch_one("SELECT * FROM memory WHERE id=?", (memory_id,))


def search_active_weaknesses(k: int = 10) -> list[dict[str, Any]]:
    """W3 接入:取 effective_strength top-k 的 ACTIVE 记忆(给弱点再训练出题用)。"""
    rows = db.fetch_all("SELECT * FROM memory WHERE status=?", (STATUS_ACTIVE,))
    scored = [(r, effective_strength(r)) for r in rows]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [r for r, _ in scored[:k]]


# ---------- 一站式:从 qa_log 抽取并存储 ----------

def extract_and_store_for_session(session_id: int) -> dict[str, int]:
    """对一场面试的所有 qa 跑抽取,合并、状态机、场次上限,返回统计。

    W4 修复 #7:幂等保护——已抽取过的 qa(extracted_at 非空)跳过,避免重复入库污染记忆。
    """
    qa_rows = db.fetch_all(
        "SELECT id, question, answer, score_json, reference_answer, extracted_at "
        "FROM qa_log WHERE session_id=?",
        (session_id,),
    )
    inserted = 0
    merged = 0
    rejected = 0
    skipped = 0
    qa_ids: list[int] = []
    for qa in qa_rows:
        qa_ids.append(qa["id"])
        # 幂等:已抽过的跳过(同时做"再次访问 = 强化"语义:重新触碰会让 last_reinforced_at 更新)
        if qa.get("extracted_at"):
            # 这里可以选择:跳过(最严格)或 重新跑(强化已存在的关联记忆)
            # 选择跳过,避免 strength 异常累加
            skipped += 1
            continue
        try:
            score = json.loads(qa.get("score_json") or "{}")
        except Exception:
            score = {}
        qa_full = {
            "id": qa["id"],
            "question": qa["question"],
            "answer": qa["answer"],
            "score": score,
        }
        items = extract_from_qa(qa_full)
        if not items:
            # 仍标记为已抽(避免空结果被反复重试)
            db.execute("UPDATE qa_log SET extracted_at=datetime('now') WHERE id=?", (qa["id"],))
            rejected += 1
            continue
        for it in items:
            new_id = add_memory(
                type_="WEAKNESS",
                content=it["content"],
                source_qa_id=qa["id"],
            )
            row = get_memory(new_id)
            if row and row["source_qa_id"] == qa["id"] and row["reinforce_count"] == 0 and row["strength"] == 1.0:
                inserted += 1
            else:
                merged += 1
        # 标记已抽
        db.execute("UPDATE qa_log SET extracted_at=datetime('now') WHERE id=?", (qa["id"],))
    capped = enforce_session_cap(qa_ids)
    idle = refresh_idle_statuses()
    return {
        "qa_count": len(qa_rows),
        "qa_extracted": len(qa_rows) - rejected - skipped,
        "qa_rejected": rejected,
        "qa_skipped": skipped,
        "inserted": inserted,
        "merged": merged,
        "capped_idle": capped,
        "idle_refreshed": idle,
    }
