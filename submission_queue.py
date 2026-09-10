"""
Очередь предложки: пользователи присылают фото (+опционально текст),
заявки уходят админу на ручное одобрение, одобренные постятся в канал.
"""

import json
from pathlib import Path
from typing import Dict, Optional

from storage import data_path

QUEUE_FILE = data_path("submissions.json")


def _load() -> Dict[str, dict]:
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save(data: Dict[str, dict]) -> None:
    QUEUE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_submission(photo_file_id: str, text: str, submitter_chat_id: int) -> str:
    data = _load()
    existing_ids = [int(k) for k in data.keys() if k.isdigit()]
    new_id = str(max(existing_ids, default=0) + 1)
    data[new_id] = {
        "photo_file_id": photo_file_id,
        "text": text,
        "submitter_chat_id": submitter_chat_id,
        "status": "pending",
    }
    _save(data)
    return new_id


def get_submission(sub_id: str) -> Optional[dict]:
    return _load().get(sub_id)


def set_status(sub_id: str, status: str) -> None:
    data = _load()
    if sub_id in data:
        data[sub_id]["status"] = status
        _save(data)


def set_text(sub_id: str, text: str) -> None:
    data = _load()
    if sub_id in data:
        data[sub_id]["text"] = text
        _save(data)


def set_admin_message_id(sub_id: str, message_id: int) -> None:
    data = _load()
    if sub_id in data:
        data[sub_id]["admin_message_id"] = message_id
        _save(data)
