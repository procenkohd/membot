"""
Простая статистика: сколько уникальных пользователей работали с ботом в месяце.
Хранится в stats.json — держим только последние 2 месяца, чтобы файл не рос
бесконечно.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from storage import data_path

STATS_FILE = data_path("stats.json")


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _load() -> Dict[str, List[int]]:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save(data: Dict[str, List[int]]) -> None:
    STATS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _users_key(month: str) -> str:
    # Отдельный namespace не смешивает старые chat_id с новыми user_id.
    return f"users:{month}"


def track(user_id: int) -> None:
    """Отметить подписанного пользователя активным в текущем месяце."""
    now = datetime.now(timezone.utc)
    key = _users_key(_month_key(now))
    data = _load()

    users = set(data.get(key, []))
    if user_id not in users:
        users.add(user_id)
        data[key] = list(users)

        keep = {key}
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        keep.add(_users_key(f"{prev_year:04d}-{prev_month:02d}"))
        data = {k: v for k, v in data.items() if k in keep}

        _save(data)


def monthly_active_count() -> int:
    """Сколько уникальных пользователей было активно в календарном месяце."""
    now = datetime.now(timezone.utc)
    key = _users_key(_month_key(now))
    data = _load()
    return len(data.get(key, []))
