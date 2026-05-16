from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from .config import Config

_config: Config | None = None

# ──────────────────────────────────────────────────────────────
# DB 初始化
# ──────────────────────────────────────────────────────────────

_DB_PATH = os.environ.get("STATE_DB_PATH", "./state.db")
_db_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        db_path = Path(_DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS invalid_issues (
                issue_id   INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS pre_verifications (
                issue_id   INTEGER NOT NULL,
                role       TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (issue_id, role)
            );

            CREATE TABLE IF NOT EXISTS violations (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
                operator       TEXT    NOT NULL,
                operator_name  TEXT    NOT NULL,
                violation_type TEXT    NOT NULL,
                project        TEXT    NOT NULL DEFAULT '',
                description    TEXT    NOT NULL,
                detail         TEXT    NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_violations_created_at
                ON violations (created_at);
            """
        )
        _conn.commit()
    return _conn


# ──────────────────────────────────────────────────────────────
# Config 管理
# ──────────────────────────────────────────────────────────────

def set_config(config: Config) -> None:
    global _config
    _config = config


def get_config() -> Config:
    if _config is None:
        raise RuntimeError("Config not initialized")
    return _config


# ──────────────────────────────────────────────────────────────
# 不合规 Issue 状态
# ──────────────────────────────────────────────────────────────

def mark_issue_invalid(issue_id: int) -> None:
    with _db_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO invalid_issues (issue_id) VALUES (?)",
            (issue_id,),
        )
        conn.commit()


def mark_issue_valid(issue_id: int) -> None:
    with _db_lock:
        conn = _get_conn()
        conn.execute("DELETE FROM invalid_issues WHERE issue_id = ?", (issue_id,))
        conn.commit()


def is_issue_known_invalid(issue_id: int) -> bool:
    with _db_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT 1 FROM invalid_issues WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        return row is not None


# ──────────────────────────────────────────────────────────────
# Pre 验收状态
# ──────────────────────────────────────────────────────────────

def mark_pre_pass(issue_id: int, role: str) -> None:
    """role: 'product' 或 'developer'"""
    with _db_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO pre_verifications (issue_id, role) VALUES (?, ?)",
            (issue_id, role),
        )
        conn.commit()


def get_pre_status(issue_id: int) -> set[str]:
    """返回该 issue 已通过的角色集合，例如 {'product'} 或 {'product', 'developer'}"""
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT role FROM pre_verifications WHERE issue_id = ?", (issue_id,)
        ).fetchall()
        return {row["role"] for row in rows}


def get_incomplete_pre_verifications() -> list[int]:
    """返回只完成了一方验收（未双向通过）的 issue_id 列表"""
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            """
            SELECT issue_id
            FROM pre_verifications
            GROUP BY issue_id
            HAVING COUNT(DISTINCT role) < 2
            """
        ).fetchall()
        return [row["issue_id"] for row in rows]


def clear_pre_verifications(issue_id: int) -> None:
    with _db_lock:
        conn = _get_conn()
        conn.execute(
            "DELETE FROM pre_verifications WHERE issue_id = ?", (issue_id,)
        )
        conn.commit()


# ──────────────────────────────────────────────────────────────
# 违规记录
# ──────────────────────────────────────────────────────────────

def record_violation(
    operator: str,
    operator_name: str,
    violation_type: str,
    description: str,
    project: str = "",
    detail: dict | None = None,
) -> int:
    with _db_lock:
        conn = _get_conn()
        cur = conn.execute(
            """
            INSERT INTO violations
                (operator, operator_name, violation_type, project, description, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                operator,
                operator_name,
                violation_type,
                project,
                description,
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def list_violations(start: str, end: str) -> list[dict]:
    """
    按时间范围查询违规记录。
    start / end 格式：'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS'
    """
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            """
            SELECT id, created_at, operator, operator_name,
                   violation_type, project, description, detail
            FROM violations
            WHERE created_at >= ? AND created_at <= ?
            ORDER BY created_at DESC
            """,
            (start, end + " 23:59:59" if len(end) == 10 else end),
        ).fetchall()
        return [dict(row) for row in rows]
