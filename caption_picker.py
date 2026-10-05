"""Подписи по теме первые три раза, затем общая колода без повторов на фото."""

import asyncio
import hashlib
import json
import sqlite3
import time

import photo_topics
from phrase_categories import ALL
from phrasebank import get_random_phrase
from storage import data_path

DB_FILE = data_path("caption_sessions.sqlite3")
THEMATIC_ATTEMPTS = 3
PHOTO_HISTORY_LIMIT = 100
# Ограничивает одновременно декодируемые фото и очереди inference на CPU.
_analysis_gate = asyncio.Semaphore(1)


def _connect():
    db = sqlite3.connect(DB_FILE)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS photos (
        chat_id INTEGER, user_id INTEGER, photo_key TEXT,
        topics TEXT NOT NULL, attempts INTEGER NOT NULL, seen TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (chat_id, user_id, photo_key)
    )""")
    return db


def _read(key):
    db = _connect()
    try:
        return db.execute(
            "SELECT topics, attempts, seen FROM photos WHERE chat_id=? AND user_id=? AND photo_key=?",
            key,
        ).fetchone()
    finally:
        db.close()


def reset_chat(chat_id: int) -> None:
    db = _connect()
    try:
        with db:
            db.execute("DELETE FROM photos WHERE chat_id=?", (chat_id,))
    finally:
        db.close()


async def choose_caption(image_bytes: bytes, chat_id: int, user_id: int,
                         category: str = ALL, previous_phrase: str | None = None) -> str:
    # По содержимому, а не render_id: повторные клики под любым старым
    # результатом для того же фото разделяют историю и бюджет трёх попыток.
    key = (chat_id, user_id, hashlib.sha256(image_bytes).hexdigest())
    row = _read(key)
    if row is None:
        async with _analysis_gate:
            row = _read(key)
            if row is None:
                topics = await asyncio.to_thread(photo_topics.analyze_topics, image_bytes)
                # Между выбором фразы и сохранением нет await: два клика
                # в одном процессе бота не выберут одну и ту же подпись.
                return _select_and_save(key, topics, category, previous_phrase)
    return _select_and_save(key, json.loads(row[0]), category, previous_phrase)


def _select_and_save(key, topics, category, previous_phrase):
    db = _connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT topics, attempts, seen FROM photos WHERE chat_id=? AND user_id=? AND photo_key=?",
                key,
            ).fetchone()
            attempts, seen = 0, []
            if row:
                topics, attempts, seen = json.loads(row[0]), row[1], json.loads(row[2])
            elif previous_phrase:
                # Кнопка мема, созданного до обновления, уже показала одну фразу.
                attempts = 1
            if previous_phrase and previous_phrase not in seen:
                seen.append(previous_phrase)
            phrase = get_random_phrase(
                key[0], category,
                preferred_topics=topics if attempts < THEMATIC_ATTEMPTS else (),
                exclude=seen,
            )
            if phrase in seen:
                seen = []  # разрешаем новый круг только после исчерпания базы
            seen.append(phrase)
            db.execute("""INSERT INTO photos VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id, photo_key) DO UPDATE SET
                topics=excluded.topics, attempts=excluded.attempts,
                seen=excluded.seen, updated_at=excluded.updated_at""",
                (*key, json.dumps(topics), attempts + 1,
                 json.dumps(seen, ensure_ascii=False), time.time_ns()))
            db.execute("""DELETE FROM photos WHERE rowid IN (
                SELECT rowid FROM photos WHERE chat_id=? AND user_id=?
                ORDER BY updated_at DESC LIMIT -1 OFFSET ?)""",
                (*key[:2], PHOTO_HISTORY_LIMIT))
            return phrase
    finally:
        db.close()
