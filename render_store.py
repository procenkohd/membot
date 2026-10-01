"""Постоянное хранилище данных кнопок под сгенерированными мемами."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

from storage import data_path


DB_FILE = data_path("renders.sqlite3")
RENDER_HISTORY_LIMIT = 20


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_FILE)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS renders (
            render_id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            payload TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    return db


def save(render_id: str, chat_id: int, user_id: int, entry: dict) -> None:
    """Сохраняет рендер и оставляет только последние записи пользователя."""
    db = _connect()
    try:
        db.execute(
            """
            INSERT INTO renders(render_id, chat_id, user_id, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(render_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                user_id=excluded.user_id,
                payload=excluded.payload,
                created_at=excluded.created_at
            """,
            (render_id, chat_id, user_id,
             json.dumps(entry, ensure_ascii=False, separators=(",", ":")),
             time.time_ns()),
        )
        db.execute(
            """
            DELETE FROM renders
            WHERE render_id IN (
                SELECT render_id FROM renders
                WHERE chat_id=? AND user_id=?
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (chat_id, user_id, RENDER_HISTORY_LIMIT),
        )
        db.commit()
    finally:
        db.close()


def get(render_id: str, chat_id: int, user_id: int) -> Optional[dict]:
    db = _connect()
    try:
        row = db.execute(
            """
            SELECT payload FROM renders
            WHERE render_id=? AND chat_id=? AND user_id=?
            """,
            (render_id, chat_id, user_id),
        ).fetchone()
    finally:
        db.close()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None
