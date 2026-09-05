"""router 共享依赖:模板实例 / profile_id 解析 / 当前 profile 加载。"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

# 单一模板实例(避免在每个 router 里重复 Jinja2Templates())
ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))


def resolve_profile_id(pid_raw) -> int | None:
    """解析前端传入的 profile_id;空/非法 → None(取最新档案)。"""
    if pid_raw in (None, "", "0", 0):
        return None
    try:
        return int(pid_raw)
    except (ValueError, TypeError):
        return None
