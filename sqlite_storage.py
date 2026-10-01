"""Небольшое постоянное FSM-хранилище для aiogram.

MemoryStorage теряет незаконченные сценарии и данные кнопок при каждом
перезапуске. Для одного polling-процесса полноценный Redis избыточен, поэтому
сохраняем JSON-состояние в SQLite рядом с остальными файлами DATA_DIR.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey


class SQLiteStorage(BaseStorage):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS fsm (
                key TEXT PRIMARY KEY,
                state TEXT,
                data TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._db.commit()
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(key: StorageKey) -> str:
        return json.dumps(asdict(key), sort_keys=True, separators=(",", ":"))

    async def set_state(self, key: StorageKey, state: State | str | None = None) -> None:
        value = state.state if isinstance(state, State) else state
        async with self._lock:
            self._db.execute(
                """
                INSERT INTO fsm(key, state, data) VALUES (?, ?, '{}')
                ON CONFLICT(key) DO UPDATE SET state=excluded.state
                """,
                (self._key(key), value),
            )
            if value is None:
                self._db.execute(
                    "DELETE FROM fsm WHERE key=? AND data='{}'", (self._key(key),)
                )
            self._db.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._lock:
            row = self._db.execute(
                "SELECT state FROM fsm WHERE key=?", (self._key(key),)
            ).fetchone()
        return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            self._db.execute(
                """
                INSERT INTO fsm(key, state, data) VALUES (?, NULL, ?)
                ON CONFLICT(key) DO UPDATE SET data=excluded.data
                """,
                (self._key(key), payload),
            )
            if not data:
                self._db.execute(
                    "DELETE FROM fsm WHERE key=? AND state IS NULL", (self._key(key),)
                )
            self._db.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._lock:
            row = self._db.execute(
                "SELECT data FROM fsm WHERE key=?", (self._key(key),)
            ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return {}

    async def close(self) -> None:
        async with self._lock:
            self._db.close()
