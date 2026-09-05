"""题库:从 seed_questions.json 加载,提供按 category / jd_keyword 检索与查重。"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from ..config import SEED_QUESTIONS_PATH

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def load_bank() -> list[dict[str, Any]]:
    if not SEED_QUESTIONS_PATH.exists():
        log.warning("seed file not found: %s", SEED_QUESTIONS_PATH)
        return []
    with open(SEED_QUESTIONS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data


def by_category(cat: str) -> list[dict[str, Any]]:
    return [q for q in load_bank() if q.get("category") == cat]


def search_by_jd_keywords(keywords: list[str], limit: int = 20) -> list[dict[str, Any]]:
    """岗位新知题:JD 关键词命中任一即返回,按命中数排序。"""
    if not keywords:
        return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for q in load_bank():
        qk = q.get("jd_keywords", [])
        hit = len(set(keywords) & set(qk))
        if hit:
            scored.append((hit, q))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [q for _, q in scored[:limit]]


def get_by_id(qid: str) -> dict[str, Any] | None:
    for q in load_bank():
        if q.get("id") == qid:
            return q
    return None
