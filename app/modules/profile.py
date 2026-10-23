"""profile:简历 + JD → 能力档案(LLM 抽取,JSON Schema 强约束)。

支持档案历史:每次生成保存为一条记录(name 可命名),可列出 / 查看 / 删除。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .. import db
from ..llm import extract_profile

log = logging.getLogger(__name__)


def create_or_update(resume_text: str, jd_text: str, name: str = "") -> int:
    """插入新档案;返回 profile.id。每次导入新建一条历史记录。"""
    skills: dict[str, Any] = {"raw_resume": resume_text, "raw_jd": jd_text}
    try:
        skills = extract_profile(resume_text, jd_text)
    except Exception as e:
        log.warning("extract_profile failed, fallback to raw: %s", e)
        # 记录失败原因,模板据此显示错误横幅,而不是把空档案伪装成"正常解析结果"
        skills["_parse_error"] = str(e)
    return db.execute(
        "INSERT INTO profile(name, resume_text, parsed_skills_json, target_jd) VALUES (?,?,?,?)",
        (name, resume_text, json.dumps(skills, ensure_ascii=False), jd_text),
    )


def _hydrate(row: dict[str, Any]) -> dict[str, Any]:
    try:
        row["parsed_skills"] = json.loads(row.pop("parsed_skills_json") or "{}")
    except Exception:
        row["parsed_skills"] = {}
    return row


def get_latest() -> dict[str, Any] | None:
    rows = db.fetch_all("SELECT * FROM profile ORDER BY id DESC LIMIT 1")
    if not rows:
        return None
    return _hydrate(rows[0])


def get_profile(profile_id: int) -> dict[str, Any] | None:
    row = db.fetch_one("SELECT * FROM profile WHERE id=?", (profile_id,))
    return _hydrate(row) if row else None


def list_profiles(limit: int = 50) -> list[dict[str, Any]]:
    """档案历史列表(概览字段)。"""
    rows = db.fetch_all(
        "SELECT id, name, target_jd, created_at, resume_text, parsed_skills_json FROM profile ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            parsed = json.loads(r.get("parsed_skills_json") or "{}")
        except Exception:
            parsed = {}
        out.append({
            "id": r["id"],
            "name": r.get("name") or f"档案 #{r['id']}",
            "created_at": r.get("created_at"),
            "target_jd": (r.get("target_jd") or "")[:80],
            "resume_preview": (r.get("resume_text") or "")[:80],
            "skill_count": len(parsed.get("skills") or []),
            "project_count": len(parsed.get("projects") or []),
        })
    return out


def delete_profile(profile_id: int) -> None:
    db.execute("DELETE FROM profile WHERE id=?", (profile_id,))


def get_parsed_skills(profile_id: int | None = None) -> dict[str, Any]:
    """指定 profile_id 则取该档案,否则取最新一条。"""
    if profile_id is not None:
        p = get_profile(profile_id)
    else:
        p = get_latest()
    if not p:
        return {}
    return p.get("parsed_skills", {})
