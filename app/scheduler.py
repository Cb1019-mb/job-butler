"""APScheduler:周日 23:59 跑周报生成;启动时挂载。"""
from __future__ import annotations

import datetime as _dt
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db
from .modules import report as _report

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _job_run_weekly_report() -> None:
    try:
        result = _report.generate(save=True)
        log.info("周报已生成: week_start=%s", result["week_start"])
    except Exception as e:
        log.error("周报生成失败: %s", e)


def start() -> None:
    """挂载后台调度器(W1/W2/W3 不调,start 幂等)。"""
    global _scheduler
    if _scheduler is not None:
        return
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    # 周日 23:59 跑
    sched.add_job(
        _job_run_weekly_report,
        CronTrigger(day_of_week="sun", hour=23, minute=59),
        id="weekly_report",
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    log.info("APScheduler 启动:周报每周日 23:59 生成")


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
