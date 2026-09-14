"""
Очередь предложки фраз: пользователь присылает текст, заявка уходит
админу на ручное одобрение, одобренные попадают в базу фраз.
"""

import json
from pathlib import Path
from typing import Dict, Optional

from storage import data_path

QUEUE_FILE = data_path("phrase_submissions.json")


def _load() -> Dict[str, dict]:
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save(data: Dict[str, dict]) -> None:
    QUEUE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_submission(text: str, submitter_chat_id: int) -> str:
    data = _load()
    existing_ids = [int(k) for k in data.keys() if k.isdigit()]
    new_id = str(max(existing_ids, default=0) + 1)
    data[new_id] = {
        "text": text,
        "submitter_chat_id": submitter_chat_id,
        "status": "pending",
    }
    _save(data)
    return new_id


def get_submission(sub_id: str) -> Optional[dict]:
    return _load().get(sub_id)


def has_pending(text: str) -> bool:
    """Такая фраза уже ждёт модерации? Чтобы один и тот же текст из своих
    мемов не копился в очереди по десять раз."""
    key = " ".join(text.casefold().replace("ё", "е").split())
    return any(
        sub.get("status") == "pending"
        and " ".join(sub.get("text", "").casefold().replace("ё", "е").split()) == key
        for sub in _load().values()
    )


def set_status(sub_id: str, status: str) -> None:
    data = _load()
    if sub_id in data:
        data[sub_id]["status"] = status
        _save(data)


def set_admin_message_id(sub_id: str, message_id: int) -> None:
    data = _load()
    if sub_id in data:
        data[sub_id]["admin_message_id"] = message_id
        _save(data)
