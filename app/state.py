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
            DROP TABLE IF EXISTS invalid_issues;
            DROP TABLE IF EXISTS pre_verifications;

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

            CREATE TABLE IF NOT EXISTS hotfix_sync_pending (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
                project_id    INTEGER NOT NULL,
                project       TEXT    NOT NULL DEFAULT '',
                mr_iid        INTEGER NOT NULL,
                mr_title      TEXT    NOT NULL DEFAULT '',
                mr_url        TEXT    NOT NULL DEFAULT '',
                operator      TEXT    NOT NULL DEFAULT '',
                operator_name TEXT    NOT NULL DEFAULT ''
            );
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


def record_hotfix_sync_pending(
    project_id: int,
    project: str,
    mr_iid: int,
    mr_title: str,
    mr_url: str,
    operator: str,
    operator_name: str,
) -> int:
    with _db_lock:
        conn = _get_conn()
        cur = conn.execute(
            """
            INSERT INTO hotfix_sync_pending
                (project_id, project, mr_iid, mr_title, mr_url, operator, operator_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, project, mr_iid, mr_title, mr_url, operator, operator_name),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def clear_hotfix_sync_pending(project_id: int) -> int:
    """删除指定项目的所有待同步记录，返回删除行数。"""
    with _db_lock:
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM hotfix_sync_pending WHERE project_id = ?",
            (project_id,),
        )
        conn.commit()
        return cur.rowcount


def list_overdue_hotfix_syncs(threshold_hours: int = 4) -> list[dict]:
    """返回超时未同步的记录（created_at 距今超过 threshold_hours 小时）。"""
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            """
            SELECT id, created_at, project_id, project, mr_iid, mr_title, mr_url,
                   operator, operator_name
            FROM hotfix_sync_pending
            WHERE created_at <= datetime('now', 'localtime', ? || ' hours')
            ORDER BY created_at ASC
            """,
            (f"-{threshold_hours}",),
        ).fetchall()
        return [dict(row) for row in rows]


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
