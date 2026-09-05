"""SQLite 封装:WAL + busy_timeout + 单写者队列 + FTS5(trigram) + sqlite-vec。

9 张表:
profile / interview_session / qa_log / session_question / memory / application /
weekly_report / settings / token_usage
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import sqlite_vec

from .config import DB_PATH


_writer_lock = threading.Lock()


def _enable_vec(conn: sqlite3.Connection) -> None:
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=5.0,
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    _enable_vec(conn)
    return conn


_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS profile (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT '',
        resume_text TEXT NOT NULL,
        parsed_skills_json TEXT NOT NULL DEFAULT '{}',
        target_jd TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS interview_session (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role_tag TEXT NOT NULL,
        company TEXT,
        mode TEXT NOT NULL DEFAULT 'MOCK',
        profile_id INTEGER REFERENCES profile(id) ON DELETE SET NULL,
        summary_md TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES interview_session(id) ON DELETE CASCADE,
        question TEXT NOT NULL,
        question_source TEXT NOT NULL,
        answer TEXT NOT NULL DEFAULT '',
        score_json TEXT NOT NULL DEFAULT '{}',
        reference_answer TEXT NOT NULL DEFAULT '',
        extracted_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS session_question (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES interview_session(id) ON DELETE CASCADE,
        qa_index INTEGER NOT NULL,
        question TEXT NOT NULL,
        question_source TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        reference_answer TEXT NOT NULL DEFAULT '',
        scoring_points_json TEXT NOT NULL DEFAULT '[]',
        UNIQUE(session_id, qa_index)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        content TEXT NOT NULL,
        source_qa_id INTEGER REFERENCES qa_log(id) ON DELETE SET NULL,
        embedding BLOB,
        half_life_days REAL NOT NULL DEFAULT 14.0,
        strength REAL NOT NULL DEFAULT 1.0,
        reinforce_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_reinforced_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS application (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company TEXT NOT NULL,
        role TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT '投递',
        stage_updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        notes TEXT NOT NULL DEFAULT ''
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS weekly_report (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        week_start TEXT NOT NULL,
        content_md TEXT NOT NULL,
        memory_cited TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS token_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purpose TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qa_session ON qa_log(session_id);
    CREATE INDEX IF NOT EXISTS idx_memory_status ON memory(status);
    CREATE INDEX IF NOT EXISTS idx_app_stage ON application(stage);
    """,
]


def init_db() -> None:
    """启动时初始化。"""
    with _writer_lock:
        conn = _connect()
        try:
            for stmt in _SCHEMA:
                conn.executescript(stmt)
            _migrate(conn)
            _ensure_fts_and_vec(conn)
        finally:
            conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移:为已存在的旧表补新列(CREATE TABLE IF NOT EXISTS 不会改已有表)。"""
    sess_cols = {r["name"] for r in conn.execute("PRAGMA table_info(interview_session)").fetchall()}
    if "profile_id" not in sess_cols:
        conn.execute("ALTER TABLE interview_session ADD COLUMN profile_id INTEGER REFERENCES profile(id) ON DELETE SET NULL")
    if "summary_md" not in sess_cols:
        conn.execute("ALTER TABLE interview_session ADD COLUMN summary_md TEXT")
    prof_cols = {r["name"] for r in conn.execute("PRAGMA table_info(profile)").fetchall()}
    if "name" not in prof_cols:
        conn.execute("ALTER TABLE profile ADD COLUMN name TEXT NOT NULL DEFAULT ''")


def _ensure_fts_and_vec(conn: sqlite3.Connection) -> None:
    """FTS5 用 trigram tokenizer(中文不退化);sqlite-vec 虚拟表用于 memory 检索。"""
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS qa_log_fts USING fts5(
            question,
            answer,
            content='qa_log',
            content_rowid='id',
            tokenize='trigram'
        );
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS qa_log_ai AFTER INSERT ON qa_log BEGIN
            INSERT INTO qa_log_fts(rowid, question, answer)
            VALUES (new.id, new.question, new.answer);
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS qa_log_ad AFTER DELETE ON qa_log BEGIN
            INSERT INTO qa_log_fts(qa_log_fts, rowid, question, answer)
            VALUES ('delete', old.id, old.question, old.answer);
        END;
        """
    )


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """读连接,任意线程可用。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_conn() -> Iterator[sqlite3.Connection]:
    """写连接:WAL + busy_timeout + 单写者锁(够用,文档对齐"单写者线程")。"""
    with _writer_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()


def fetch_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def fetch_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> int:
    with write_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or 0


def execute_script(sql: str) -> None:
    with write_conn() as conn:
        conn.executescript(sql)


# ---------- settings(kv 表) ----------

def get_setting(key: str, default: str | None = None) -> str | None:
    row = fetch_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES (?,?,datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
        """,
        (key, value),
    )


def all_settings() -> dict[str, str]:
    rows = fetch_all("SELECT key, value FROM settings")
    return {r["key"]: r["value"] for r in rows}
