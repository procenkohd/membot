"""
Управление базой фраз и "непроходящим" рандомом:
каждому чату — своя перемешанная колода, которая не повторяется,
пока не закончится, а потом тасуется заново.
"""

import hashlib
import json
import random
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from phrase_categories import ALL, CATEGORIES, DECK_CATEGORIES, classify_phrase
from storage import data_path

PHRASES_FILE = Path(__file__).parent / "phrases.txt"
UNCENSORED_PHRASES_FILE = Path(__file__).parent / "phrases_uncensored.txt"
USER_PHRASES_FILE = data_path("phrases_user.txt")
STATE_FILE = data_path("state.json")

_categorized_cache_key: tuple | None = None
_categorized_cache: Dict[str, List[str]] | None = None


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


def _source_signature() -> tuple:
    signature = []
    for path in (PHRASES_FILE, USER_PHRASES_FILE, UNCENSORED_PHRASES_FILE):
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signature.append((str(path), None, None))
    return tuple(signature)


def load_categorized_phrases() -> Dict[str, List[str]]:
    """Загружает всю базу и назначает каждой фразе ровно одну категорию."""
    global _categorized_cache_key, _categorized_cache
    cache_key = _source_signature()
    if cache_key == _categorized_cache_key and _categorized_cache is not None:
        return {name: phrases.copy() for name, phrases in _categorized_cache.items()}

    result = {category: [] for category in CATEGORIES}
    for phrase in _read_lines(PHRASES_FILE) + _read_lines(USER_PHRASES_FILE):
        result[classify_phrase(phrase)].append(phrase)
    for phrase in _read_lines(UNCENSORED_PHRASES_FILE):
        result[classify_phrase(phrase)].append(phrase)
    _categorized_cache_key = cache_key
    _categorized_cache = {name: phrases.copy() for name, phrases in result.items()}
    return result


def load_phrases(category: Optional[str] = None) -> List[str]:
    """
    Читает базу фраз заново при каждом вызове — можно дополнять файлы на лету.
    Собирается из phrases.txt, редакционной пачки phrases_uncensored.txt и
    phrases_user.txt (фразы, одобренные через бота). Рубрикация выполняется
    при чтении, поэтому новые пользовательские строки сразу оказываются в
    подходящей колоде.
    """
    categorized = load_categorized_phrases()
    all_phrases = [phrase for name in CATEGORIES for phrase in categorized[name]]
    if category is None or category == ALL:
        return all_phrases
    return categorized.get(category, all_phrases).copy()


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


def _pool_signature(phrases: List[str]) -> str:
    payload = "\0".join(phrases).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def get_random_phrase(chat_id: int, category: str = ALL) -> str:
    """
    Возвращает случайную фразу для конкретного чата так, чтобы фразы
    не повторялись, пока не будет пройдена вся база. При изменении
    базы (добавлении новых строк) колода пересобирается автоматически.
    """
    if category not in DECK_CATEGORIES:
        category = ALL
    phrases = load_phrases(category)
    if not phrases and category != ALL:
        category = ALL
        phrases = load_phrases(category)
    if not phrases:
        return "тут пусто|как и внутри"

    state = _load_state()
    chat_key = str(chat_id)
    chat_state = state.get(chat_key, {})
    decks = chat_state.get("decks", {})
    category_state = decks.get(category, {})

    deck = category_state.get("deck", [])
    signature = _pool_signature(phrases)

    # Старая state.json мигрирует сама: в ней нет словаря decks.
    if not deck or category_state.get("signature") != signature:
        deck = list(range(len(phrases)))
        random.shuffle(deck)

    index = deck.pop()
    phrase = phrases[index] if index < len(phrases) else random.choice(phrases)

    decks[category] = {"deck": deck, "signature": signature}
    state[chat_key] = {"decks": decks}
    _save_state(state)

    return phrase


def add_phrase(new_phrase: str) -> None:
    """Дописывает одобренную админом фразу в phrases_user.txt (не в общую phrases.txt,
    чтобы не потерять её при перезаливке общей базы файлом)."""
    with USER_PHRASES_FILE.open("a", encoding="utf-8") as f:
        f.write("\n" + new_phrase.strip())


def phrase_count() -> int:
    return len(load_phrases())


def phrase_counts_by_category() -> Dict[str, int]:
    return {name: len(items) for name, items in load_categorized_phrases().items()}
