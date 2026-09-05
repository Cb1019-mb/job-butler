"""application 看板:增删改查 + 阶段流转。"""
from __future__ import annotations

import json
from typing import Any

from .. import db

STAGES = ["投递", "笔试", "面试", "offer", "挂"]
STAGE_NEXT: dict[str, list[str]] = {
    "投递": ["笔试", "面试", "挂"],
    "笔试": ["面试", "挂"],
    "面试": ["offer", "挂"],
    "offer": [],
    "挂":   [],
}


def list_applications() -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT * FROM application ORDER BY stage_updated_at DESC, id DESC"
    )


def create_application(company: str, role: str, notes: str = "") -> int:
    return db.execute(
        """
        INSERT INTO application(company, role, stage, stage_updated_at, notes)
        VALUES (?, ?, '投递', datetime('now'), ?)
        """,
        (company, role, notes),
    )


def get_application(app_id: int) -> dict[str, Any] | None:
    return db.fetch_one("SELECT * FROM application WHERE id=?", (app_id,))


def update_stage(app_id: int, stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(f"invalid stage: {stage}")
    db.execute(
        "UPDATE application SET stage=?, stage_updated_at=datetime('now') WHERE id=?",
        (stage, app_id),
    )


def update_notes(app_id: int, notes: str) -> None:
    db.execute("UPDATE application SET notes=? WHERE id=?", (notes, app_id))


def delete_application(app_id: int) -> None:
    db.execute("DELETE FROM application WHERE id=?", (app_id,))


def group_by_stage() -> dict[str, list[dict[str, Any]]]:
    """返回按阶段分组的看板数据。"""
    apps = list_applications()
    groups: dict[str, list[dict[str, Any]]] = {s: [] for s in STAGES}
    for a in apps:
        if a["stage"] in groups:
            groups[a["stage"]].append(a)
        else:
            groups.setdefault(a["stage"], []).append(a)
    return groups


def stage_summary() -> dict[str, int]:
    """返回每阶段计数。"""
    rows = db.fetch_all("SELECT stage, COUNT(*) AS n FROM application GROUP BY stage")
    out = {s: 0 for s in STAGES}
    for r in rows:
        out[r["stage"]] = r["n"]
    return out
