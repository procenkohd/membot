"""Daily feature counters. No photos, message text or Telegram file IDs."""
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from storage import data_path

DB_FILE = data_path("usage.sqlite3")
try:
    TZ = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    # Minimal hosting images may omit the system IANA database.
    TZ = timezone(timedelta(hours=3), "МСК")
LABELS = {
    "random": "Рандомный мем", "custom": "Свой мем",
    "edit_text": "Изменение текста", "repic": "Другая картинка",
    "reroll:all": "Другая подпись", "reroll:absurd": "Абсурднее",
    "reroll:hard": "Жёстче", "reroll:intellectual": "Интеллектуальнее",
    "chat": "Готовая переписка", "sticker": "Добавление стикера",
    "pack": "Создание стикерпака",
    "phrase": "Фраза на модерацию", "submission": "Заявка в канал",
    "reroll:general": "Обычная подпись (старая кнопка)",
}


def _connect():
    db = sqlite3.connect(DB_FILE, timeout=1)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT, user_id INTEGER, feature TEXT, count INTEGER NOT NULL, PRIMARY KEY(day,user_id,feature))")
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT OR IGNORE INTO meta VALUES ('started', ?)",
                   (datetime.now(TZ).isoformat(timespec="seconds"),))
    except Exception:
        db.close()
        raise
    return db


def record(user_id: int, feature: str) -> None:
    """Failures in analytics must not break the bot. Exclude owner activity."""
    if not user_id or user_id == int(os.environ.get("ADMIN_ID", "0") or 0):
        return
    if feature != "active" and feature not in LABELS:
        return
    db = None
    try:
        db = _connect()
        with db:
            db.execute("INSERT INTO usage VALUES (?, ?, ?, 1) ON CONFLICT(day,user_id,feature) DO UPDATE SET count=count+1",
                       (datetime.now(TZ).date().isoformat(), user_id, feature))
    except Exception:
        logging.getLogger(__name__).exception("Usage statistics unavailable")
    finally:
        if db is not None:
            db.close()


def report(days: int = 30) -> str:
    if days not in (7, 30, 90):
        raise ValueError("Supported periods: 7, 30, 90")
    if not DB_FILE.exists():
        return "Статистика ещё не накопилась. История до обновления сюда не входит."
    today = datetime.now(TZ).date()
    start = (today - timedelta(days=days - 1)).isoformat()
    end = today.isoformat()
    admin = int(os.environ.get("ADMIN_ID", "0") or 0)
    db = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    try:
        params = (start, end, admin)
        total = db.execute("SELECT count(DISTINCT user_id) FROM usage WHERE day BETWEEN ? AND ? AND user_id != ?", params).fetchone()[0]
        rows = db.execute("SELECT feature,count(DISTINCT user_id),sum(count) FROM usage WHERE day BETWEEN ? AND ? AND user_id != ? AND feature!='active' GROUP BY feature ORDER BY count(DISTINCT user_id) DESC, sum(count) DESC, feature", params).fetchall()
        started = db.execute("SELECT value FROM meta WHERE key='started'").fetchone()
    finally:
        db.close()
    lines = [f"Статистика {start} — {end} (МСК, сегодня неполный день)",
             f"Сбор начат: {started[0] if started else 'неизвестно'}",
             f"Активных пользователей: {total}. Администратор исключён.", "",
             "Функция: людей · доля активных · успешных действий"]
    for feature, users, count in rows:
        lines.append(f"{LABELS.get(feature, feature)}: {users} · {users / total:.0%} · {count}")
    if not rows:
        lines.append("Успешных действий пока нет.")
    lines.append("\nОдин человек может пользоваться несколькими функциями. Старые данные не восстановлены.")
    return "\n".join(lines)
