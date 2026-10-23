"""LLM 任务层:5 个高阶任务函数 + 各自 schema/prompt。

与 `app.llm` 的分工:
- `app.llm`: 底层 OpenAI 客户端、思考模式、kwargs 构造、token 记录
- `app.llm_tasks`: 具体业务任务(profile / judge / 出题 / 综合点评),用 `app.llm._chat` + `app.json_utils.run_json_task`

调用方只 import 这里的函数,不再 import `app.llm._chat` 等私有函数。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Iterator

from . import llm as _llm
from .json_utils import (
    ensure_judge_citations,
    extract_json,
    judge_looks_like_placeholder,
    normalize_keys,
    run_json_task,
    validate_schema,
)

log = logging.getLogger(__name__)


# ---------- 1. 能力档案抽取 ----------

PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": "候选人会的技术关键词(从简历和 JD 抽取)",
        },
        "projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "tech": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "summary", "tech"],
            },
            "description": "简历中的项目,用于后续深挖出题",
        },
        "weak_signals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "疑似短板(技能薄弱或经验缺位)",
        },
        "jd_focus": {
            "type": "array",
            "items": {"type": "string"},
            "description": "JD 重点能力词",
        },
    },
    "required": ["skills", "projects", "weak_signals", "jd_focus"],
}


PROFILE_SYSTEM = """你是资深求职教练,擅长从简历和 JD 提取结构化能力档案。

硬性格式要求(必须严格遵守):
1. skills 必须是字符串数组(如 ["Python", "FastAPI"]),不要用对象分类
2. jd_focus 必须是字符串数组(从 JD 提取的关键词),不要用对象
3. weak_signals 必须是字符串数组(候选人的短板/可改进点),不要用对象
4. projects 必须是对象数组,每个对象有 name / summary / tech 三个字段:
   - name: 项目名
   - summary: 一句话描述
   - tech: 技术栈字符串数组
5. 不要输出顶层 name / education / year / target_job 等其他字段
6. 只输出严格符合上述格式的 JSON,不要任何解释或 markdown 包裹,不要在 JSON 外加任何文字"""


def extract_profile(resume_text: str, jd_text: str) -> dict[str, Any]:
    user = (
        f"【简历】\n{resume_text}\n\n"
        f"【目标 JD】\n{jd_text}\n\n"
        "请按 Schema 输出 JSON。"
    )
    return run_json_task(
        _llm._chat,
        system=PROFILE_SYSTEM,
        user=user,
        schema=PROFILE_SCHEMA,
        kind="profile",
        purpose="profile",
    )


# ---------- 2. 三轴点评 ----------

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tech_correctness": {
            "type": "object",
            "properties": {
                "score": {"type": "number", "minimum": 0, "maximum": 5},
                "comment": {"type": "string"},
                "cited_points": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["score", "comment", "cited_points"],
        },
        "completeness": {
            "type": "object",
            "properties": {
                "score": {"type": "number", "minimum": 0, "maximum": 5},
                "comment": {"type": "string"},
                "missed_points": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["score", "comment", "missed_points"],
        },
        "structure": {
            "type": "object",
            "properties": {
                "score": {"type": "number", "minimum": 0, "maximum": 5},
                "comment": {"type": "string"},
                "suggestion": {"type": "string"},
            },
            "required": ["score", "comment", "suggestion"],
        },
        "overall": {"type": "string", "description": "一句话总评"},
    },
    "required": ["tech_correctness", "completeness", "structure", "overall"],
}


JUDGE_SYSTEM = """你是严格的 AI 面试官,按三轴 rubric 给候选人的作答评分,每轴 0~5 分。

三轴(任何题目都用这套,不要换维度):
- tech_correctness(技术正确性):答案里的技术事实是否准确(0=完全错,5=完全正确)
- completeness(完整性):相对参考答案 + 采分点,候选人答了几成(0=几乎没覆盖,5=全部覆盖)
- structure(表达结构):逻辑是否清晰(STAR/分层/总分总),语言是否专业

硬性要求:
1. 逐条核对【采分点】:cited_points / missed_points 必须逐字取自【采分点】列表(不得自行编写或从参考答案摘抄);候选人答中的进 cited_points,未答中的进 missed_points;两者不允许同时为空
2. 分数和评语必须针对【候选人作答】的实际内容独立评判,严禁照抄下方示例中的任何分数或文字
3. 作答跑题、空话套话、或与题目无关时:三轴各给 0~1 分,cited_points 留空数组,missed_points 写入未覆盖的采分点,overall 明确指出"答非所问"
4. 不要打"总分",只给三轴分;即使题目"特殊"(项目介绍、行为面)也用同一套三轴
5. 只输出严格符合 JSON Schema 的 JSON,不要任何解释或 markdown 包裹

输出结构示例(仅演示字段类型与层级,score 是 0~5 数字、两个 points 是字符串数组;示例中的 0 分和尖括号占位符不是评分参考,照抄即为错误):
{"tech_correctness": {"score": 0, "comment": "<针对作答的一句话技术点评>", "cited_points": ["<答中的采分点原文>"], "missed_points": ["<未答中的采分点原文>"]}, "completeness": {"score": 0, "comment": "<相对采分点的覆盖程度>", "missed_points": []}, "structure": {"score": 0, "comment": "<结构与表达评价>", "suggestion": "<一条改进建议>"}, "overall": "<一句话总评>"}"""


def judge_answer(
    question: str,
    reference_answer: str,
    scoring_points: list[str],
    answer: str,
) -> dict[str, Any]:
    # 作答紧跟题目(模型对末尾与近端内容注意力最强),参考答案/采分点只作评判基准
    user = (
        f"【题目】\n{question}\n\n"
        f"【候选人作答】\n{answer}\n\n"
        f"【参考答案】\n{reference_answer}\n\n"
        f"【采分点】\n" + "\n".join(f"- {p}" for p in scoring_points) + "\n\n"
        "评分前先通读【候选人作答】原文,再逐条核对【采分点】,按 Schema 输出 JSON。"
    )
    def _reject_placeholder(data: dict[str, Any]) -> None:
        if judge_looks_like_placeholder(data):
            raise ValueError("点评照抄了输出示例,未基于实际作答评分")

    score = run_json_task(
        _llm._chat,
        system=JUDGE_SYSTEM,
        user=user,
        schema=JUDGE_SCHEMA,
        kind="judge",
        purpose="judge",
        content_check=_reject_placeholder,
    )
    return ensure_judge_citations(score, scoring_points)


def judge_answer_stream(
    question: str,
    reference_answer: str,
    scoring_points: str,
    answer: str,
) -> Iterator[str]:
    """流式版点评:逐 chunk 推 JSON 文本片段(简单透传,前端用 JS 拼接)。"""
    # 与 judge_answer 的段落顺序保持一致:作答紧跟题目
    user = (
        f"【题目】\n{question}\n\n"
        f"【候选人作答】\n{answer}\n\n"
        f"【参考答案】\n{reference_answer}\n\n"
        f"【采分点】\n{scoring_points}\n\n"
        "评分前先通读【候选人作答】原文,再逐条核对【采分点】,按 Schema 输出 JSON。"
    )
    kwargs = _llm._build_chat_kwargs(
        [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": user},
        ],
        stream=True,
    )
    stream = _llm.get_client().chat.completions.create(**kwargs)
    for chunk in stream:
        # include_usage 下,最后一个 chunk choices 为空但带 usage
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            _llm._record_usage(usage, "judge_stream", kwargs["model"])
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta


# ---------- 2.5 作答相关性拦截(跑题/空话不过 LLM,直接给 0 分点评) ----------

def _text_tokens(text: str) -> set[str]:
    """CJK 2-gram + 拉丁词(≥2 字符),用于粗糙的词面相关性比对。

    注意不能用 str.isalnum() 判拉丁词:它对汉字也返回 True(Unicode 感知),
    会把 "AOF记录写命令" 整段吞成一个 token。
    """
    text = re.sub(r"\s+", "", text or "")
    tokens: set[str] = set()
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if "\u4e00" <= ch <= "\u9fff":
            if i + 1 < n:
                tokens.add(text[i:i + 2])
            i += 1
        elif ch.isascii() and ch.isalnum():
            j = i
            while j < n and text[j].isascii() and text[j].isalnum():
                j += 1
            if j - i >= 2:
                tokens.add(text[i:j].lower())
            i = j
        else:
            i += 1
    return tokens


def answer_is_irrelevant(answer: str, question: str, reference_answer: str, scoring_points: list[str]) -> bool:
    """作答与题目/参考答案/采分点几乎无词面重叠时判定为跑题或空话。

    只拦退化的无效作答(占位文本、空话套话),避免 4B 模型把它们幻觉成高分点评;
    命中任意领域词的正常回答(哪怕很短)都放行走 LLM 点评。
    """
    ans = (answer or "").strip()
    if len(ans) < 6:
        return True
    corpus = _text_tokens(f"{question} {reference_answer} {' '.join(scoring_points)}")
    if not corpus:
        return False
    atoks = _text_tokens(ans)
    if not atoks:
        return True
    return len(atoks & corpus) / len(atoks) < 0.08


def build_irrelevant_score(scoring_points: list[str]) -> dict[str, Any]:
    """跑题作答的确定性 0 分点评(missed_points 带全部采分点,保证有据可查)。"""
    return {
        "tech_correctness": {
            "score": 0,
            "comment": "作答与题目无关或为空话,无技术内容可评估",
            "cited_points": [],
            "missed_points": list(scoring_points),
        },
        "completeness": {"score": 0, "comment": "未覆盖任何采分点", "missed_points": []},
        "structure": {"score": 0, "comment": "无有效内容结构可评估", "suggestion": "围绕题目要求组织作答,先结论后展开"},
        "overall": "答非所问:作答内容与题目无关",
    }


# ---------- 3. 出题(简历深挖 + 岗位新知,W1 用) ----------

QUESTION_GEN_SCHEMA: dict[str, "dict[str, Any]"] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "reference_answer": {"type": "string"},
                    "scoring_points": {"type": "array", "items": {"type": "string"}},
                    "category": {"type": "string"},
                },
                "required": ["question", "reference_answer", "scoring_points", "category"],
            },
        }
    },
    "required": ["questions"],
}


def _build_question_user(project: dict[str, Any], n: int, avoid: list[str] | None) -> str:
    user = (
        f"项目名:{project.get('name','')}\n"
        f"项目摘要:{project.get('summary','')}\n"
        f"技术栈:{','.join(project.get('tech', []))}\n\n"
        f"请生成 {n} 道项目深挖面试题,每道题带参考答案和 3~4 个采分点。"
    )
    if avoid:
        user += "\n\n【避免重复】以下题目近期已考过,请换角度出题,不要出语义相同的题:\n" + "\n".join(f"- {q}" for q in avoid)
    return user


def _build_targeted_user(topics: list[str], n: int, avoid: list[str] | None, context: str) -> str:
    user = (
        (f"【考察背景】{context}\n\n" if context else "")
        + "【考察目标】\n" + "\n".join(f"- {t}" for t in topics) + "\n\n"
        f"请生成**恰好 {n} 道**不同的面试题,每道题对应一个不同的考察目标(不要把多个目标合并进一道题),每道题带参考答案和 3~4 个采分点。"
    )
    if avoid:
        user += "\n\n【避免重复】以下题目近期已考过,不要出语义相同的题,换角度提问:\n" + "\n".join(f"- {q}" for q in avoid[:12])
    return user


def generate_resume_questions(project: dict[str, Any], n: int = 2, avoid: list[str] | None = None) -> list[dict[str, Any]]:
    """从单个项目生成 n 道深挖题;avoid 为近期已问题面,提示模型换角度避免重复。"""
    system = "你是 AI 面试官,根据项目经历出深挖题。只输出严格符合 Schema 的 JSON。"
    data = run_json_task(
        _llm._chat,
        system=system,
        user=_build_question_user(project, n, avoid),
        schema=QUESTION_GEN_SCHEMA,
        kind="question",
        purpose="question_gen",
    )
    return data.get("questions", [])[:n]


# ---------- 4. 定向出题(弱点再训练 / 岗位新知) ----------

def generate_targeted_questions(
    topics: list[str],
    n: int = 1,
    avoid: list[str] | None = None,
    context: str = "",
) -> list[dict[str, Any]]:
    """针对薄弱知识点 / JD 关键词现场生成 n 道题(带参考答案与采分点)。

    avoid 为近期已问题面,要求模型换角度,保证每场题目不同。
    """
    if not topics:
        return []
    system = "你是 AI 面试官,针对给定的考察目标出针对性面试题。只输出严格符合 Schema 的 JSON。"
    data = run_json_task(
        _llm._chat,
        system=system,
        user=_build_targeted_user(topics, n, avoid, context),
        schema=QUESTION_GEN_SCHEMA,
        kind="question",
        purpose="question_gen",
    )
    return [q for q in data.get("questions", []) if q.get("question")][:n]


# ---------- 5. 会话综合点评 ----------

SUMMARY_SYSTEM = """你是资深求职教练,基于一场模拟面试的全部题目与三轴评分,写一份综合复盘。

输出 Markdown,包含四个小节(不要输出其他内容):
## 整体表现
## 做得好
## 待改进
## 下场建议

要求:
1. 引用具体题目与三轴分数作为依据,不要空话;
2. "待改进"按严重程度排序,最多 4 条,每条对应到具体知识点;
3. "下场建议"给出可执行的 1~3 条动作(练什么、怎么练)。"""


def _format_summary_user(qa_items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, it in enumerate(qa_items, 1):
        s = it.get("score") or {}

        def _axis(k: str) -> str:
            sub = s.get(k) or {}
            v = sub.get("score")
            return f"{v}/5" if isinstance(v, (int, float)) else "未评分"

        lines.append(
            f"### 题 {i}: {it.get('question','')}\n"
            f"- 作答: {(it.get('answer') or '')[:400]}\n"
            f"- 技术正确性 {_axis('tech_correctness')} | 完整性 {_axis('completeness')} | 表达结构 {_axis('structure')}\n"
            f"- 技术点评: {(s.get('tech_correctness') or {}).get('comment','')}\n"
        )
    return "以下是本场全部题目与评分:\n\n" + "\n".join(lines) + "\n\n请输出综合复盘 Markdown。"


def generate_session_summary(qa_items: list[dict[str, Any]]) -> str:
    """整场综合点评:输入 [{question, answer, score}],返回 Markdown 文本。"""
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            return _llm._chat(
                [
                    {"role": "system", "content": SUMMARY_SYSTEM},
                    {"role": "user", "content": _format_summary_user(qa_items)},
                ],
                purpose="summary",
            )
        except Exception as e:
            last_err = e
            log.warning("session summary attempt %s failed: %s", attempt + 1, e)
    raise RuntimeError(f"综合点评生成失败: {last_err}")


# ---------- 公共导出(替代 llm.py 中的同名字) ----------

__all__ = [
    "PROFILE_SCHEMA", "PROFILE_SYSTEM", "extract_profile",
    "JUDGE_SCHEMA", "JUDGE_SYSTEM", "judge_answer", "judge_answer_stream",
    "answer_is_irrelevant", "build_irrelevant_score",
    "QUESTION_GEN_SCHEMA", "generate_resume_questions", "generate_targeted_questions",
    "SUMMARY_SYSTEM", "generate_session_summary",
]
