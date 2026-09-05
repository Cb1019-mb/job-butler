"""FastAPI 主入口。

- startup 预热 embedding + 挂载 APScheduler
- 8 个 router 子文件:`pages` / `profile` / `interview` / `memory` / `kanban` / `report` / `settings` / `mcp_http`
- 业务路由细节见 `app/routers/`
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import embed, scheduler
from .db import init_db
from .routers import kanban as _r_kanban
from .routers import mcp_http as _r_mcp
from .routers import memory as _r_memory
from .routers import pages as _r_pages
from .routers import profile as _r_profile
from .routers import report as _r_report
from .routers import settings as _r_settings
from .routers import interview as _r_interview

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("job_butler")

ROOT = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        embed.warmup()
    except Exception as e:
        log.warning("embedding warmup failed: %s", e)
    try:
        scheduler.start()
    except Exception as e:
        log.warning("APScheduler 启动失败: %s", e)
    yield
    try:
        scheduler.shutdown()
    except Exception:
        pass


app = FastAPI(title="求职管家", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

# 路由分组(改动路由只动对应文件)
app.include_router(_r_pages.router)
app.include_router(_r_profile.router)
app.include_router(_r_interview.router)
app.include_router(_r_memory.router)
app.include_router(_r_kanban.router)
app.include_router(_r_report.router)
app.include_router(_r_settings.router)
app.include_router(_r_mcp.router)
