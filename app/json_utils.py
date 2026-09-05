"""LLM 输出解析公共工具。

所有需要从模型输出里"取 JSON + 归一化 + 校验"的任务都通过 `run_json_task` 走,避免每个任务函数重复写 retry loop。

提供:
- `extract_json(text)`: 三层兜底(整体 / ```json 块 / brace-balance)
- `normalize_keys(data, kind)`: 三种 schema 归一化(judge / profile / question)
- `validate_schema(data, schema)`: 轻量必填 + 类型 + score 范围
- `run_json_task(...)`: 标准 4 步流水线
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)


# ---------- 抽取 JSON ----------

def extract_json(text: str) -> Any:
    """从模型输出中抽 JSON(可能包在 ```json ... ``` 里)。

    三层兜底:
    1. 整体就是 JSON
    2. 抓 ```json ... ``` 代码块
    3. brace-balance 找最外层完整 JSON(避免被中间示例或嵌套 JSON 干扰)
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if esc:
                esc = False
                continue
            if c == "\\" and in_str:
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise json.JSONDecodeError("no valid JSON object found", text, 0)


# ---------- 归一化 ----------

_JUDGE_AXES = ("tech_correctness", "completeness", "structure")
_JUDGE_AXIS_FILL_KEYS = {
    "tech_correctness": ("comment", "cited_points"),
    "completeness":     ("comment", "missed_points"),
    "structure":         ("comment", "suggestion"),
}
_JUDGE_AXIS_ALIAS = {
    "accuracy": "tech_correctness", "tech": "tech_correctness",
    "comp": "completeness",
    "clarity": "structure",
}


def _normalize_judge(data: dict[str, Any]) -> dict[str, Any]:
    """宽容键名:模型可能用 accuracy/clarity 替代 tech_correctness/structure。

    同时把顶层 'score' dict 拆成嵌套 {axis: {score, comment}}。
    """
    # 1) 拆顶层 score 子结构
    flat = data.get("score")
    if isinstance(flat, dict) and "tech_correctness" not in data:
        for src, dst in _JUDGE_AXIS_ALIAS.items():
            if src in flat and dst not in data:
                v = flat[src]
                if isinstance(v, (int, float)):
                    data[dst] = {"score": v, "comment": "", "cited_points": [], "missed_points": []}
                elif isinstance(v, dict):
                    inner = v.get("score", 0)
                    data[dst] = {
                        "score": inner if isinstance(inner, (int, float)) else 0,
                        "comment": v.get("comment", "") if isinstance(v.get("comment"), str) else "",
                        "cited_points": v.get("cited_points", []) if isinstance(v.get("cited_points"), list) else [],
                        "missed_points": v.get("missed_points", []) if isinstance(v.get("missed_points"), list) else [],
                    }
    # 2) 顶层 cited_points / missed_points 归并
    if "tech_correctness" in data and isinstance(data["tech_correctness"], dict):
        tc = data["tech_correctness"]
        if "cited_points" not in tc and "cited_points" in data:
            tc["cited_points"] = data.pop("cited_points") or []
        if "missed_points" not in tc and "missed_points" in data:
            tc["missed_points"] = data.pop("missed_points") or []
    # 3) 三轴补 0;裸数字时保留分数
    for axis in _JUDGE_AXES:
        v = data.get(axis)
        if isinstance(v, dict):
            if "score" not in v:
                v["score"] = 0
            elif not isinstance(v.get("score"), (int, float)):
                v["score"] = 0
        elif isinstance(v, (int, float)):
            data[axis] = {"score": v, "comment": "", "cited_points": [], "missed_points": []}
        else:
            data[axis] = {"score": 0, "comment": "未评分", "cited_points": [], "missed_points": []}
    # 4) overall
    if "overall" not in data or not isinstance(data.get("overall"), str):
        avg = (data["tech_correctness"]["score"]
               + data["completeness"]["score"]
               + data["structure"]["score"]) / 3
        data["overall"] = f"三轴均分 {avg:.2f}/5"
    # 5) 子字段保底
    for axis, missing_keys in _JUDGE_AXIS_FILL_KEYS.items():
        ax = data[axis]
        for k in missing_keys:
            if k not in ax:
                ax[k] = "" if k in ("comment", "suggestion") else []
    return data


def _normalize_profile(data: dict[str, Any]) -> dict[str, Any]:
    """补 skills/jd_focus/weak_signals/projects 缺失字段;projects 内部字段补全。"""
    for k in ("skills", "jd_focus", "weak_signals"):
        if k not in data or not isinstance(data[k], list):
            data[k] = []
    if "projects" not in data or not isinstance(data["projects"], list):
        data["projects"] = []
    for p in data["projects"]:
        if not isinstance(p, dict):
            continue
        p.setdefault("name", "")
        p.setdefault("summary", "")
        if not isinstance(p.get("tech"), list):
            p["tech"] = []
    return data


def _normalize_question(data: dict[str, Any]) -> dict[str, Any]:
    """补 questions 数组;每个 q 字段补全;scoring_points 统一转字符串列表。"""
    if "questions" not in data or not isinstance(data["questions"], list):
        data["questions"] = []
    for q in data["questions"]:
        if not isinstance(q, dict):
            continue
        q.setdefault("question", "")
        q.setdefault("reference_answer", "")
        q.setdefault("scoring_points", [])
        q.setdefault("category", "")
        if not isinstance(q.get("scoring_points"), list):
            q["scoring_points"] = []
        # 采分点统一转字符串(模型可能输出 dict / int)
        sps: list[str] = []
        for p in q["scoring_points"]:
            if isinstance(p, str):
                s = p.strip()
            elif isinstance(p, dict):
                s = "; ".join(f"{k}: {v}" for k, v in p.items() if v) or str(p)
            else:
                s = str(p)
            if s:
                sps.append(s)
        q["scoring_points"] = sps
    return data


_NORMALIZERS = {
    "judge": _normalize_judge,
    "profile": _normalize_profile,
    "question": _normalize_question,
}


def normalize_keys(data: Any, kind: str = "judge") -> Any:
    """按 kind 选归一化函数。"""
    if not isinstance(data, dict):
        return data
    fn = _NORMALIZERS.get(kind)
    return fn(data) if fn else data


# ---------- Schema 校验 ----------

def validate_schema(data: Any, schema: dict[str, Any]) -> None:
    """轻量校验:必填字段、类型、score 范围。"""
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    for key in schema.get("required", []):
        if key not in data:
            raise ValueError(f"缺字段: {key}")
    props = schema.get("properties", {})
    for k, v in data.items():
        if k not in props:
            continue
        sub = props[k]
        if sub.get("type") == "object" and isinstance(v, dict):
            validate_schema(v, sub)
        elif sub.get("type") == "array" and not isinstance(v, list):
            raise ValueError(f"{k} 应为数组")
        elif sub.get("type") == "number" and not isinstance(v, (int, float)):
            raise ValueError(f"{k} 应为数字")
        elif sub.get("type") == "string" and not isinstance(v, str):
            raise ValueError(f"{k} 应为字符串")
    # score 范围
    for axis in _JUDGE_AXES:
        if axis in data and "score" in data[axis]:
            s = data[axis]["score"]
            if not (0 <= s <= 5):
                raise ValueError(f"{axis}.score 越界: {s}")


# ---------- schema_hint 提示 ----------

def schema_hint(schema: dict[str, Any], attempt: int) -> str:
    """第 N 次重试时附加给 user 的 schema 强约束提示。"""
    if attempt == 0:
        return ""
    return (
        "\n\n注意:上一轮输出未通过 Schema 校验。请只输出一个 JSON 对象,"
        "键名必须严格匹配 Schema,不要任何解释、不要 markdown 包裹、不要省略必需字段。"
    )


# ---------- 4 步流水线 ----------

ChatFn = Callable[..., str]


def run_json_task(
    chat: ChatFn,
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    kind: str = "judge",
    max_attempts: int = 2,
    purpose: str = "llm",
) -> dict[str, Any]:
    """标准 4 步流水线:chat → extract_json → normalize → validate;失败重试。

    Args:
        chat: 实际发请求的函数,签名 (messages, *, temperature=None, purpose=...) -> str。
              调用方一般传 `app.llm._chat` 包装(传 purpose 由内部完成)。
        system: system prompt
        user:   user prompt(不含 schema_hint,内部会加)
        schema: 必填字段 + 类型定义
        kind:   normalize 类型("judge" / "profile" / "question")
        max_attempts: 重试次数
        purpose: 日志标签
    """
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            text = chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user + schema_hint(schema, attempt)},
                ],
                purpose=purpose,
            )
            data = normalize_keys(extract_json(text), kind)
            validate_schema(data, schema)
            return data
        except Exception as e:
            last_err = e
            log.warning("%s attempt %s failed: %s", purpose, attempt + 1, e)
    raise RuntimeError(f"{purpose} 失败: {last_err}")


# ---------- 异常安全包装 ----------

def safe_call(default: Any, msg: str, fn, *args, **kwargs) -> Any:
    """统一"吞异常 + 返回默认"包装:替代 15+ 处 `try/except Exception as e: log.warning(...); return default`。

    使用场景:辅助逻辑(记录 token / 记录 usage / 调 LLM 抽取)失败不应阻塞主流程。
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log.warning("%s: %s", msg, e)
        return default
