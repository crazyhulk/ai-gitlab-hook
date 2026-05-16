from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from .config import Config

_config: Config | None = None

# ──────────────────────────────────────────────────────────────
# 配置
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
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invalid_issues (
                issue_id   INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
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
# 不合规 Issue 状态（SQLite 持久化）
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
