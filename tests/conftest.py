"""共享测试 fixture:sys.path、db 隔离、临时 db 路径。

约定:
- 所有测试用 `tmp_db` fixture(自动清空)避免污染主 db
- `clean_db` fixture 直接清空当前 job_butler.db(不删除文件)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def clean_db():
    """清空 job_butler.db 的全部表,但不删文件(让 settings 等 DEFAULTS 在).get() 时落库)。"""
    from app import db
    from app.db import init_db
    init_db()
    for table in ("memory", "qa_log", "session_question", "interview_session", "application",
                  "weekly_report", "profile", "token_usage"):
        try:
            db.execute(f"DELETE FROM {table}")
        except Exception:
            pass
    yield db


@pytest.fixture
def reset_settings():
    """把所有 settings 重置回 DEFAULTS,避免跨测试污染。"""
    from app.modules import settings as _settings
    _settings.reset_to_defaults()
    yield _settings
