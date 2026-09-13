"""
Блок фраз, распознанных с картинок мемов (проект meme-ocr), в phrases.txt.

Фразы лежат в конце phrases.txt между строками-маркерами START и END. Для бота
маркеры — обычные #-комментарии, а блок можно целиком перезалить или удалить:

    python ocr_phrases.py import <файл.jsonl>   # записать блок (если уже есть — заменить)
    python ocr_phrases.py remove                # вырезать блок вместе с маркерами

Вход — jsonl из meme-ocr после finalize.py: по записи на картинку, {"file", "text", ...}
либо {"file", "error"}; в text строки мема идут сверху вниз через \\n.

Перед любой записью phrases.txt копируется в backups/. Всё вне блока остаётся байт
в байт: файл читается и пишется байтами, переводы строк в блоке — как в самом файле.
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

PHRASES_FILE = Path(__file__).parent / "phrases.txt"
BACKUP_DIR = Path(__file__).parent / "backups"

START = "# >>> OCR-мемы: начало"
END = "# <<< OCR-мемы: конец"

# Пороги подобраны по снимку на 2784 записи и режут только явный мусор, а не ошибки OCR.
# dict_rate из jsonl не используется: он плохо отличает ошибки OCR от мата и сленга.
MIN_WORDS = 2               # одно слово — обычно обрывок надписи или вотермарка
MIN_CYRILLIC = 6            # меньше — обрывки вроде "чТо П0.12.0315"
MIN_CYRILLIC_SHARE = 0.7    # доля кириллицы среди букв; ниже — английский или каша со скриншота
MAX_LENGTH = 300            # длиннее — уже не подпись, а текст скриншота; в базе самая длинная ~190
MIXED_SCRIPT_MIN_LETTERS = 4    # слово короче — совпадение латиницы/кириллицы может быть случайным
IRREGULAR_CASE_MIN_LETTERS = 6  # слово короче — "Ты"/"ИЗ" не должны триггерить

CYRILLIC = re.compile(r"[а-яёА-ЯЁіїєґўІЇЄҐЎ]")
CYRILLIC_CHARS = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґўАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯІЇЄҐЎ")
CYRILLIC_UPPER_CHARS = set("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯІЇЄҐЎ")
LATIN_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
GREEK = re.compile(r"[Α-Ωα-ω]")
BANK_UI_MARKERS = ("перевод выполнен", "карта списания", "карта зачисления", "баланс", "отправить справку")


def to_phrase(text: str) -> str:
    """Текст мема -> одна строка для phrases.txt."""
    phrase = " ".join(text.split())        # строки мема через пробел, пробелы схлопнуты
    phrase = phrase.replace("|", "/")      # | у бота делит фразу на верх/низ
    return re.sub(r"^[#\s]+", "", phrase)  # строку с # в начале бот считает комментарием


def _has_mixed_script_word(phrase: str) -> bool:
    """Слово одновременно с кириллицей и латиницей (не только "похожие" буквы, а вперемешку) —
    типичный сбой OCR: Ы/Ь распознаётся как латинские b/bI, вотермарки сайтов лезут в подпись."""
    for word in phrase.split():
        letters = [ch for ch in word if ch.isalpha()]
        if len(letters) < MIXED_SCRIPT_MIN_LETTERS:
            continue
        if any(ch in CYRILLIC_CHARS for ch in letters) and any(ch in LATIN_CHARS for ch in letters):
            return True
    return False


def _has_irregular_case_word(phrase: str) -> bool:
    """Слово вроде "хУдОЖникоМ": регистр скачет внутри слова хаотично, а не как "Слово"/"СЛОВО" —
    OCR-шум от стилизованного шрифта, а не осознанный "мокающий" стиль (тот бы держался всей фразы)."""
    for word in phrase.split():
        letters = [ch for ch in word if ch.isalpha()]
        if len(letters) < IRREGULAR_CASE_MIN_LETTERS or not all(ch in CYRILLIC_CHARS for ch in letters):
            continue
        upper_flags = [ch in CYRILLIC_UPPER_CHARS for ch in letters]
        # первую букву не считаем — "Слово" не должно триггерить
        switches = sum(1 for a, b in zip(upper_flags[1:], upper_flags[2:]) if a != b)
        if switches >= 2:
            return True
    return False


def _is_bank_ui_screenshot(phrase: str) -> bool:
    """Скриншот банковского приложения (перевод/баланс/карта), а не подпись к мему."""
    low = phrase.lower()
    return sum(1 for marker in BANK_UI_MARKERS if marker in low) >= 2


def reject_reason(phrase: str):
    """Почему фраза не годится, или None, если годится."""
    words = [w for w in phrase.split() if any(ch.isalpha() for ch in w)]
    if len(words) < MIN_WORDS:
        return f"меньше {MIN_WORDS} слов"
    cyrillic = len(CYRILLIC.findall(phrase))
    if cyrillic < MIN_CYRILLIC:
        return f"меньше {MIN_CYRILLIC} кириллических букв"
    if cyrillic / sum(ch.isalpha() for ch in phrase) < MIN_CYRILLIC_SHARE:
        return f"кириллицы меньше {MIN_CYRILLIC_SHARE:.0%} букв"
    if len(phrase) > MAX_LENGTH:
        return f"длиннее {MAX_LENGTH} символов"
    if GREEK.search(phrase):
        return "греческие буквы (сбой OCR)"
    if _is_bank_ui_screenshot(phrase):
        return "скриншот банковского приложения, а не мем"
    if _has_mixed_script_word(phrase):
        return "кириллица вперемешку с латиницей внутри слова (сбой OCR)"
    if _has_irregular_case_word(phrase):
        return "хаотичный регистр внутри слова (сбой OCR)"
    return None


def dedup_key(phrase: str) -> str:
    """Форма для поиска дублей: регистр, ё/е, пунктуация и пробелы не различаются."""
    s = phrase.casefold().replace("ё", "е")
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def bot_phrases(text: str) -> list:
    """Фразы так, как их читает бот (phrasebank._read_lines): пустые и #-строки пропускаются."""
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def collect(jsonl: Path, existing_keys: set):
    """Читает jsonl -> (фразы, отброшенные фильтром [(причина, фраза)], счётчики)."""
    stats = Counter()
    phrases, rejected, seen = [], [], set()
    with jsonl.open(encoding="utf-8-sig") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats["битых строк"] += 1  # например, недописанная последняя строка живого файла
                continue
            stats["записей"] += 1
            if "error" in rec:
                stats["с ошибкой OCR"] += 1
                continue
            phrase = to_phrase(rec.get("text") or "")
            if not phrase:
                stats["пустой text"] += 1
                continue
            reason = reject_reason(phrase)
            if reason:
                rejected.append((reason, phrase))
                continue
            key = dedup_key(phrase)
            if key in existing_keys:
                stats["уже есть в phrases.txt"] += 1
            elif key in seen:
                stats["повтор внутри jsonl"] += 1
            else:
                seen.add(key)
                phrases.append(phrase)
    return phrases, rejected, stats


def split_block(text: str):
    """Делит текст на (до блока, блок с маркерами, после блока); None, если блока нет."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, l in enumerate(lines) if l.strip() == START]
    ends = [i for i, l in enumerate(lines) if l.strip() == END]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        sys.exit(f"ошибка: маркеры блока повреждены (начал: {len(starts)}, концов: {len(ends)}), "
                 f"{PHRASES_FILE.name} не тронут — поправь маркеры руками")
    s, e = starts[0], ends[0]
    return "".join(lines[:s]), "".join(lines[s:e + 1]), "".join(lines[e + 1:])


def ends_with_newline(s: str) -> bool:
    return s.endswith(("\n", "\r"))


def save(old_bytes: bytes, new_text: str) -> None:
    """Бэкап текущего phrases.txt в backups/ и запись нового содержимого."""
    BACKUP_DIR.mkdir(exist_ok=True)
    # микросекунды — чтобы имена сортировались по времени; "xb" — старый бэкап не перезапишется
    backup = BACKUP_DIR / f"{PHRASES_FILE.name}.{datetime.now():%Y%m%d-%H%M%S-%f}.bak"
    with backup.open("xb") as f:
        f.write(old_bytes)
    PHRASES_FILE.write_bytes(new_text.encode("utf-8"))
    print(f"бэкап: {backup}")


def cmd_import(jsonl: Path) -> None:
    if not jsonl.is_file():
        sys.exit(f"ошибка: нет файла {jsonl}")
    old_bytes = PHRASES_FILE.read_bytes()
    text = old_bytes.decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    parts = split_block(text)
    outside = parts[0] + parts[2] if parts else text

    phrases, rejected, stats = collect(jsonl, {dedup_key(p) for p in bot_phrases(outside)})
    dups = stats["уже есть в phrases.txt"] + stats["повтор внутри jsonl"]

    print(f"записей в {jsonl.name}: {stats['записей']}"
          + (f" (битых строк пропущено: {stats['битых строк']})" if stats["битых строк"] else ""))
    print(f"  с ошибкой OCR: {stats['с ошибкой OCR']}, пустой text: {stats['пустой text']}")
    reasons = ", ".join(f"{r}: {c}" for r, c in Counter(r for r, _ in rejected).most_common())
    print(f"  отброшено фильтром: {len(rejected)}" + (f" ({reasons})" if reasons else ""))
    print(f"  дублей: {dups} (уже есть в phrases.txt: {stats['уже есть в phrases.txt']}, "
          f"повтор внутри jsonl: {stats['повтор внутри jsonl']})")
    print(f"  фраз в блоке: {len(phrases)}")
    if not phrases:
        sys.exit(f"ошибка: ни одной фразы, {PHRASES_FILE.name} не тронут")

    block = eol.join([
        START,
        "# Текст, распознанный с картинок мемов (проект meme-ocr). Блок пишет ocr_phrases.py — "
        "руками не править, перезаливка затрёт.",
        f"# Источник: {jsonl.name}, записей: {stats['записей']}. Дата импорта: {date.today():%Y-%m-%d}.",
        f"# Фраз: {len(phrases)} (отброшено фильтром: {len(rejected)}, дублей: {dups}).",
        "# Перезалить: python ocr_phrases.py import <файл.jsonl>",
        "# Удалить блок целиком: python ocr_phrases.py remove",
        *phrases,
        END,
    ])

    if parts:
        before, old_block, after = parts
        # хвостовой перевод строки блока оставляем прежним, чтобы remove работал одинаково
        new_text = before + block + old_block[len(old_block.rstrip("\r\n")):] + after
    elif not text or ends_with_newline(text):
        new_text = text + block + eol
    else:
        # файл кончается без перевода строки: ставим его перед маркером, а после блока — нет,
        # чтобы remove вернул файл байт в байт
        new_text = text + eol + block

    if new_text == text:
        print(f"блок не изменился, {PHRASES_FILE.name} не тронут")
        return
    save(old_bytes, new_text)
    print("блок заменён" if parts else "блок дописан в конец файла")


def cmd_clean() -> None:
    """
    Повторно прогоняет уже лежащий в phrases.txt блок через reject_reason() —
    для ретроактивной чистки после того как фильтр стал строже (например,
    научился ловить вперемешку латиницу/кириллицу и хаотичный регистр внутри
    слова — типичные сбои OCR, а не осознанные опечатки для юмора).
    """
    old_bytes = PHRASES_FILE.read_bytes()
    text = old_bytes.decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    parts = split_block(text)
    if not parts:
        sys.exit(f"блока в {PHRASES_FILE.name} нет, нечего чистить")
    before, block, after = parts

    from itertools import takewhile
    header = list(takewhile(lambda l: l.strip().startswith("#"), block.splitlines()))
    phrases = bot_phrases(block)

    kept, removed = [], []
    for p in phrases:
        reason = reject_reason(p)
        (removed if reason else kept).append((reason, p) if reason else p)

    print(f"фраз в блоке: {len(phrases)}")
    if not removed:
        print("чистить нечего, все фразы блока проходят текущий фильтр")
        return

    reasons = Counter(r for r, _ in removed)
    print(f"будет удалено: {len(removed)} ({', '.join(f'{r}: {c}' for r, c in reasons.most_common())})")
    print(f"останется: {len(kept)}")

    BACKUP_DIR.mkdir(exist_ok=True)
    report = BACKUP_DIR / f"ocr_removed.{datetime.now():%Y%m%d-%H%M%S-%f}.txt"
    report.write_text(
        "\n".join(f"[{reason}] {phrase}" for reason, phrase in removed) + "\n",
        encoding="utf-8",
    )
    print(f"отчёт по удалённым фразам: {report}")

    new_block = eol.join([
        *header,
        f"# Чистка (ocr_phrases.py clean, {date.today():%Y-%m-%d}): было {len(phrases)}, "
        f"удалено {len(removed)}, осталось {len(kept)}. Отчёт: {report.name} в backups/.",
        *kept,
        END,
    ])
    new_text = before + new_block + block[len(block.rstrip("\r\n")):] + after
    save(old_bytes, new_text)
    print("блок почищен")


def cmd_remove() -> None:
    old_bytes = PHRASES_FILE.read_bytes()
    text = old_bytes.decode("utf-8")
    parts = split_block(text)
    if not parts:
        print(f"блока в {PHRASES_FILE.name} нет, файл не тронут")
        return
    before, block, after = parts
    if not after and not ends_with_newline(block) and ends_with_newline(before):
        # блок дописывался к файлу без перевода строки в конце — убираем и тот, что добавил import
        before = before[:-2] if before.endswith("\r\n") else before[:-1]
    save(old_bytes, before + after)
    print(f"блок удалён: {len(bot_phrases(block))} фраз")


def main() -> None:
    parser = argparse.ArgumentParser(description="блок фраз из OCR мемов в phrases.txt")
    sub = parser.add_subparsers(dest="command", required=True)
    p_import = sub.add_parser("import", help="записать блок из jsonl (существующий заменяется)")
    p_import.add_argument("jsonl", type=Path, help="jsonl из meme-ocr после finalize.py")
    sub.add_parser("clean", help="повторно прогнать уже импортированный блок через фильтр reject_reason")
    sub.add_parser("remove", help="вырезать блок вместе с маркерами")
    args = parser.parse_args()
    if args.command == "import":
        cmd_import(args.jsonl)
    elif args.command == "clean":
        cmd_clean()
    else:
        cmd_remove()


if __name__ == "__main__":
    main()
