"""设置:模型/出题/记忆/画面参数。

设计:
- 持久化到 DB 的:模型配置、出题参数、记忆阈值
- 仅前端 localStorage 的:主题、字号、密度、流式开关
- 修改模型配置后下一次请求生效(get_client 动态读)
"""
from __future__ import annotations

from typing import Any

from .. import db
from .. import config as _cfg

# ---------- 持久化默认值 ----------
# 单一配置源:默认值全部派生自 config.py(支持 env 覆盖),避免双源漂移

DEFAULTS: dict[str, str] = {
    # 模型
    "llm.provider":         _cfg.LLM_PROVIDER,                     # vllm / deepseek / custom
    "llm.base_url":         _cfg.VLLM_BASE_URL,
    "llm.api_key":          _cfg.VLLM_API_KEY,
    "llm.model":            _cfg.VLLM_MODEL,
    "llm.temperature":      str(_cfg.LLM_TEMPERATURE),
    "llm.timeout":          "30",
    "llm.disable_thinking": "",  # 空=按厂商预设自动;true=强制关闭;false=保留思考
    "embed.base_url":       _cfg.EMBED_BASE_URL,
    "embed.api_key":        _cfg.EMBED_API_KEY,
    "embed.model":          _cfg.BGE_MODEL_NAME,
    "embed.dim":            str(_cfg.BGE_DIM),
    # 出题
    "interview.questions_per_session": str(_cfg.QUESTIONS_PER_SESSION),
    "interview.weakness_ratio":        str(_cfg.WEAKNESS_RATIO),
    "interview.resume_ratio":          str(_cfg.RESUME_RATIO),
    "interview.new_ratio":             str(_cfg.NEW_KNOWLEDGE_RATIO),
    "interview.dedup_days":            str(_cfg.DEDUP_DAYS),
    # 记忆
    "memory.enabled":            "true",
    "memory.merge_threshold":    str(_cfg.MERGE_THRESHOLD),
    "memory.max_per_session":    str(_cfg.MAX_MEMORIES_PER_SESSION),
    "memory.half_life_weakness": str(_cfg.WEAK_HALF_LIFE_DAYS),
    "memory.half_life_fact":     str(_cfg.FACT_HALF_LIFE_DAYS),
    "memory.half_life_pref":     str(_cfg.PREF_HALF_LIFE_DAYS),
    "memory.idle_days":          "30",
}


def get(key: str) -> str:
    """读设置,缺则返回默认(并落库,保证 all_settings 完整)。"""
    val = db.get_setting(key)
    if val is not None:
        return val
    default = DEFAULTS.get(key)
    if default is None:
        return ""
    db.set_setting(key, default)
    return default


def get_int(key: str) -> int:
    try:
        return int(float(get(key)))
    except (ValueError, TypeError):
        return int(float(DEFAULTS.get(key, "0")))


def get_float(key: str) -> float:
    try:
        return float(get(key))
    except (ValueError, TypeError):
        return float(DEFAULTS.get(key, "0"))


def get_bool(key: str) -> bool:
    """真值: 'true'/'1'/'yes' → True;其他 → False。"""
    val = get(key).strip().lower()
    return val in ("true", "1", "yes", "on")


def get_all() -> dict[str, str]:
    """全部设置(key->value)。缺默认的自动补。"""
    out = dict(DEFAULTS)
    out.update(db.all_settings())
    return out


def update_many(pairs: dict[str, str]) -> None:
    for k, v in pairs.items():
        if k in DEFAULTS:  # 只接受已知 key
            db.set_setting(k, v)


def reset_to_defaults() -> None:
    """清空 settings 表后由 DEFAULTS 重建。"""
    with db.write_conn() as conn:
        conn.execute("DELETE FROM settings")
    for k, v in DEFAULTS.items():
        db.set_setting(k, v)


def as_ui_dict() -> dict[str, dict[str, Any]]:
    """给前端用的分组结构。api_key 做掩码,不回传明文。

    单一配置源:按 key 前缀自动分组(llm.* / embed.* → model,interview.* → interview,memory.* → memory)。
    任何 DEFAULTS 增删都自动反映,不需要手动维护映射。

    **key 命名规则**:
    - `model` 组里 `llm.*` / `embed.*` **必须保留前缀**(避免 llm_model / embed_model 撞名)
    - 其他组 (`interview.*` / `memory.*`) 只有 1 个前缀,直接用 short key 简洁
    """
    # (prefix, group, keep_prefix_if_same_group)
    _PREFIX_GROUP = (
        ("llm.",     "model",     True),   # 保留前缀
        ("embed.",   "model",     True),   # 保留前缀(避免撞 llm_)
        ("interview.", "interview", False),  # 唯一前缀,剥掉
        ("memory.",  "memory",    False),  # 唯一前缀,剥掉
    )

    raw = get_all()
    out: dict[str, dict[str, Any]] = {"model": {}, "interview": {}, "memory": {}}
    for full_key, val in raw.items():
        if not isinstance(full_key, str) or "." not in full_key:
            continue
        prefix, short = full_key.split(".", 1)
        for p, g, keep in _PREFIX_GROUP:
            # p 形如 "llm." 带点;prefix 不带点
            if prefix + "." == p:
                key = f"{p}{short}".replace(".", "_") if keep else short
                out[g][key] = val
                break
    # api_key 掩码
    for g in out.values():
        for k in list(g):
            if k.endswith("api_key"):
                g[k] = _mask_key(g[k])
    return out


def provider_presets() -> dict[str, dict[str, str]]:
    """LLM 厂商预设(供前端下拉自动填充 base_url/model)。"""
    from ..config import PROVIDER_PRESETS
    return PROVIDER_PRESETS


def _mask_key(key: str) -> str:
    """掩码敏感 key:空或占位 EMPTY 原样返回,其余保留末 4 位。"""
    if not key or key == "EMPTY":
        return key
    if len(key) <= 4:
        return "****"
    return "****" + key[-4:]


def token_usage_stats() -> dict[str, Any]:
    """tokens 使用统计:总量 + 按 purpose 分组 + 按天趋势(近 14 天)。"""
    total = db.fetch_one(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS pt, COALESCE(SUM(completion_tokens),0) AS ct FROM token_usage"
    ) or {"calls": 0, "pt": 0, "ct": 0}
    by_purpose = db.fetch_all(
        """
        SELECT purpose, COUNT(*) AS calls,
               SUM(prompt_tokens) AS prompt_tokens,
               SUM(completion_tokens) AS completion_tokens
        FROM token_usage GROUP BY purpose ORDER BY (SUM(prompt_tokens)+SUM(completion_tokens)) DESC
        """
    )
    by_day = db.fetch_all(
        """
        SELECT date(created_at, 'localtime') AS day,
               SUM(prompt_tokens) AS prompt_tokens,
               SUM(completion_tokens) AS completion_tokens
        FROM token_usage
        WHERE datetime(created_at, 'localtime') >= datetime('now', 'localtime', '-14 days')
        GROUP BY day ORDER BY day
        """
    )
    return {
        "total_calls": total["calls"],
        "total_prompt": total["pt"],
        "total_completion": total["ct"],
        "total_all": total["pt"] + total["ct"],
        "by_purpose": by_purpose,
        "by_day": by_day,
    }
