"""
Управление базой фраз и "непроходящим" рандомом:
каждому чату — своя перемешанная колода, которая не повторяется,
пока не закончится, а потом тасуется заново.
"""

import json
import random
from pathlib import Path
from typing import List, Tuple, Optional

from storage import data_path

PHRASES_FILE = Path(__file__).parent / "phrases.txt"
USER_PHRASES_FILE = data_path("phrases_user.txt")
STATE_FILE = data_path("state.json")


def _read_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def load_phrases() -> List[str]:
    """
    Читает базу фраз заново при каждом вызове — можно дополнять файлы на лету.
    Собирается из двух файлов: phrases.txt (общая база, можно пополнять
    файлом целиком) и phrases_user.txt (фразы, одобренные через бота) —
    так массовая загрузка файла в phrases.txt не затирает то, что уже
    добавили пользователи через бота.
    """
    return _read_lines(PHRASES_FILE) + _read_lines(USER_PHRASES_FILE)


def parse_phrase(phrase: str) -> Tuple[str, Optional[str]]:
    """
    Разбирает строку формата "верх|низ" в (top, bottom).
    Если разделителя нет — вся фраза идёт вниз, верх пустой.
    """
    if "|" in phrase:
        top, bottom = phrase.split("|", 1)
        return top.strip(), bottom.strip()
    return "", phrase.strip()


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_random_phrase(chat_id: int) -> str:
    """
    Возвращает случайную фразу для конкретного чата так, чтобы фразы
    не повторялись, пока не будет пройдена вся база. При изменении
    базы (добавлении новых строк) колода пересобирается автоматически.
    """
    phrases = load_phrases()
    if not phrases:
        return "тут пусто|как и внутри"

    state = _load_state()
    chat_key = str(chat_id)
    chat_state = state.get(chat_key, {})

    deck = chat_state.get("deck", [])
    known_count = chat_state.get("total", 0)

    # если база фраз изменилась (пополнилась) — пересобираем колоду
    if not deck or known_count != len(phrases):
        deck = list(range(len(phrases)))
        random.shuffle(deck)

    index = deck.pop()
    phrase = phrases[index] if index < len(phrases) else random.choice(phrases)

    state[chat_key] = {"deck": deck, "total": len(phrases)}
    _save_state(state)

    return phrase


def add_phrase(new_phrase: str) -> None:
    """Дописывает одобренную админом фразу в phrases_user.txt (не в общую phrases.txt,
    чтобы не потерять её при перезаливке общей базы файлом)."""
    with USER_PHRASES_FILE.open("a", encoding="utf-8") as f:
        f.write("\n" + new_phrase.strip())


def phrase_count() -> int:
    return len(load_phrases())
