"""
Ищет в phrases.txt пары фраз с одинаковым началом, но чуть разным концом —
не осознанные варианты генератора (generate.py подставляет в шаблон одно из
полусотни состояний вроде "апатия"/"скука"/"ностальгия" — это разный контент,
не дубль), а случайные повторы: одна и та же фраза с опечаткой, лишней/съеденной
буквой, не той пунктуацией или слипшимися словами — обычно потому что один и
тот же мем/шутка встретился дважды (например, разными путями пришёл в базу).

Только показывает кандидатов, ничего не меняет и не удаляет — решать, какую
из двух версий оставить, всё равно нужно на глаз (в паре не всегда очевидно,
какая версия опечатка, а какая нет).

    python find_near_duplicates.py [-n 2] [--ratio 0.95]
"""

import argparse
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from phrasebank import load_phrases

GENERATE_PY = Path(__file__).parent / "generate.py"


def load_generator_slot_values() -> list:
    """
    Достаёт из generate.py списки слов-подстановок (state_nouns, dreams, results,
    ...) без импорта модуля целиком: после строки "generated = set()" файл на
    верхнем уровне реально запускает генерацию и перезаписывает
    generated_phrases.txt, так что `import generate` делать нельзя. Выполняем
    только безопасный префикс файла (одни объявления списков и функций) и
    забираем оттуда все плоские списки строк.
    """
    src = GENERATE_PY.read_text(encoding="utf-8")
    marker = "\ngenerated = set()"
    if marker not in src:
        sys.exit(f"ошибка: в {GENERATE_PY.name} не найден ожидаемый маркер, обнови этот скрипт")
    ns = {}
    exec(compile(src[:src.index(marker)], str(GENERATE_PY), "exec"), ns)
    values = []
    for v in ns.values():
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            values.extend(v)
    return values


def normalize(phrase: str) -> str:
    s = phrase.casefold().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return " ".join(s.split())


def build_slot_word_lists(raw_slots: list) -> list:
    seen = {normalize(s) for s in raw_slots}
    seen.discard("")
    return sorted((s.split() for s in seen), key=len, reverse=True)


def strip_first_slot_match(words: list, slot_word_lists: list):
    """Вырезает первое (самое длинное) вхождение известного слота generate.py
    как непрерывного куска слов; возвращает (остаток, сам слот) или (words, None)."""
    n = len(words)
    for slot_words in slot_word_lists:
        L = len(slot_words)
        if 0 < L <= n:
            for start in range(n - L + 1):
                if words[start:start + L] == slot_words:
                    return words[:start] + words[start + L:], " ".join(slot_words)
    return words, None


def find_candidates(phrases: list, slot_word_lists: list, prefix_n: int, ratio_min: float):
    norm = [normalize(p) for p in phrases]
    words = [n.split() for n in norm]

    buckets = defaultdict(list)
    for i, w in enumerate(words):
        if len(w) >= prefix_n:
            buckets[tuple(w[:prefix_n])].append(i)

    pairs = []
    for idxs in buckets.values():
        if len(idxs) < 2:
            continue
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                if norm[i] == norm[j]:
                    pairs.append((i, j, 1.0))
                    continue
                rem_i, slot_i = strip_first_slot_match(words[i], slot_word_lists)
                rem_j, slot_j = strip_first_slot_match(words[j], slot_word_lists)
                if slot_i and slot_j and slot_i != slot_j:
                    if SequenceMatcher(None, rem_i, rem_j).ratio() >= 0.9:
                        continue  # осознанная подстановка генератора, не дубль
                ratio = SequenceMatcher(None, norm[i], norm[j]).ratio()
                if ratio >= ratio_min:
                    pairs.append((i, j, ratio))
    return pairs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--prefix-words", type=int, default=2, help="сколько первых слов считать началом (по умолчанию 2)")
    ap.add_argument("--ratio", type=float, default=0.95, help="порог схожести всей фразы (по умолчанию 0.95)")
    args = ap.parse_args()

    phrases = load_phrases()
    slot_word_lists = build_slot_word_lists(load_generator_slot_values())
    pairs = find_candidates(phrases, slot_word_lists, args.prefix_words, args.ratio)

    print(f"фраз: {len(phrases)}, кандидатов в дубли: {len(pairs)}")
    for i, j, ratio in sorted(pairs, key=lambda t: -t[2]):
        print(f"--- {ratio:.2f} ---")
        print("   ", phrases[i])
        print("   ", phrases[j])


if __name__ == "__main__":
    main()
