"""LLM 客户端底层:OpenAI 客户端 / 思考模式 / kwargs 构造 / token 记录。

设计要点(对齐文档 §十):
- 温度 0,关思考模式(若模型支持)
- 模型配置改 settings 后下一次请求生效(动态读,见 `reset_client`)
- LLM 业务任务(profile / judge / 出题 / 综合点评)在 `app.llm_tasks`
- LLM 输出解析在 `app.json_utils`

本文件只提供**底层基础设施**;业务函数由 `app.llm_tasks` 转发兼容。
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

import httpx
from openai import OpenAI

from .config import LLM_TEMPERATURE
from .modules import settings as _settings

log = logging.getLogger(__name__)

_client: OpenAI | None = None
_client_signature: tuple | None = None


# ---------- 客户端 ----------

def _sig() -> tuple:
    return (
        _settings.get("llm.provider"),
        _settings.get("llm.base_url"),
        _settings.get("llm.api_key"),
        _settings.get("llm.timeout"),
    )


def get_client() -> OpenAI:
    """所有 provider 统一走 OpenAI 兼容协议,base_url / api_key 直接读 settings。

    厂商预设只在前端下拉用于自动填充 base_url+model,运行时不再区分 provider 分支,
    因此 DeepSeek / OpenAI / Kimi / 智谱 / 通义 / vLLM / custom 均可直接接入。
    """
    global _client, _client_signature
    sig = _sig()
    if _client is not None and _client_signature == sig:
        return _client
    _client = OpenAI(
        api_key=sig[2] or "EMPTY",
        base_url=sig[1],
        timeout=float(sig[3] or 30),
        # trust_env=False:不走系统代理,防止本机代理劫持 localhost 端点(返回 404)
        http_client=httpx.Client(trust_env=False),
    )
    _client_signature = sig
    return _client


def reset_client() -> None:
    """清掉 LLM 客户端缓存,下次 `get_client()` 按当前 settings 重建。

    供 settings 路由修改 provider / base_url / api_key / timeout 后调用,避免继续用旧连接。
    """
    global _client, _client_signature
    _client = None
    _client_signature = None


def get_model_name() -> str:
    return _settings.get("llm.model")


def get_temperature() -> float:
    try:
        return float(_settings.get("llm.temperature"))
    except (ValueError, TypeError):
        return LLM_TEMPERATURE


def _thinking_extra_body() -> dict | None:
    """决定是否关闭思考模式:用户显式设置(llm.disable_thinking)优先,否则按厂商预设。

    vLLM 的 Qwen3 思考版需要 enable_thinking=false;官方云 API(DeepSeek/OpenAI 等)
    不识别该字段,传入可能报错,故 custom/云厂商默认不带(预设 thinking_off=false)。
    """
    provider = _settings.get("llm.provider")
    val = (_settings.get("llm.disable_thinking") or "").strip().lower()
    if val in ("true", "1", "yes", "on"):
        want_off = True
    elif val in ("false", "0", "no", "off"):
        want_off = False
    else:
        # 空 = 自动:跟随当前厂商预设
        from .config import PROVIDER_PRESETS
        want_off = PROVIDER_PRESETS.get(provider, {}).get("thinking_off") == "true"
    return {"chat_template_kwargs": {"enable_thinking": False}} if want_off else None


def _record_usage(usage, purpose: str, model: str) -> None:
    """把一次调用的 token 消耗写入 token_usage 表(统计失败不影响主流程)。"""
    from . import db
    from .json_utils import safe_call
    def _do():
        pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "completion_tokens", 0) or 0
        db.execute(
            "INSERT INTO token_usage(purpose, model, prompt_tokens, completion_tokens) VALUES (?,?,?,?)",
            (purpose, model, pt, ct),
        )
    safe_call(None, "record token usage failed", _do)


# ---------- 构造请求 ----------

def _build_chat_kwargs(
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """统一构造 client.chat.completions.create 的 kwargs。

    - 同步:不带 stream,带 thinking_extra_body(若需要)
    - 流式:带 stream=True,extra_body 合并 stream_options.include_usage 以便记录 tokens
    """
    kwargs: dict[str, Any] = {
        "model": get_model_name(),
        "messages": messages,
        "temperature": temperature if temperature is not None else get_temperature(),
    }
    extra = _thinking_extra_body()
    if stream:
        if extra is None:
            extra = {}
        else:
            extra = dict(extra)
        extra["stream_options"] = {"include_usage": True}
        kwargs["extra_body"] = extra
        kwargs["stream"] = True
    elif extra:
        kwargs["extra_body"] = extra
    return kwargs


def _chat(messages: list[dict[str, str]], *, temperature: float | None = None, purpose: str = "chat") -> str:
    """单轮 chat completion,返回原始文本。按 provider 预设决定是否关闭思考模式。"""
    kwargs = _build_chat_kwargs(messages, temperature=temperature)
    resp = get_client().chat.completions.create(**kwargs)
    _record_usage(getattr(resp, "usage", None), purpose, kwargs["model"])
    return (resp.choices[0].message.content or "").strip()


# ---------- 业务函数已迁到 app.llm_tasks,这里 re-export 兼容 ----------

# 通过 __getattr__ 让老代码 `from .llm import extract_profile` 仍可工作
def __getattr__(name: str):
    """向后兼容:业务函数从 app.llm_tasks 转发。"""
    if name in (
        "extract_profile",
        "judge_answer",
        "judge_answer_stream",
        "answer_is_irrelevant",
        "build_irrelevant_score",
        "generate_resume_questions",
        "generate_targeted_questions",
        "generate_session_summary",
        "PROFILE_SCHEMA", "PROFILE_SYSTEM",
        "JUDGE_SCHEMA", "JUDGE_SYSTEM",
        "QUESTION_GEN_SCHEMA", "SUMMARY_SYSTEM",
    ):
        from . import llm_tasks as _tasks
        return getattr(_tasks, name)
    if name in ("extract_json", "normalize_keys", "validate_schema", "schema_hint"):
        from . import json_utils as _ju
        return getattr(_ju, name)
    if name == "run_json_task":
        from . import json_utils as _ju
        return _ju.run_json_task
    raise AttributeError(f"module 'app.llm' has no attribute {name!r}")


__all__ = [
    # 客户端
    "get_client", "reset_client", "get_model_name", "get_temperature",
    # 请求构造
    "_chat", "_build_chat_kwargs", "_thinking_extra_body", "_record_usage",
]
