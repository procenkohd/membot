"""Выгружает картинки эмодзи из системного шрифта Apple в папку с PNG.

Зачем не подключить шрифт напрямую: в бинарных сборках Pillow нет Raqm, без
неё не работает сложное формирование текста, и эппловские флаги (🇷🇺 — это
пара региональных индикаторов) с клавишами (1️⃣) просто не собираются. Плюс
разбор таблицы sbix целиком тянет в память все 74 МБ картинок, а на хостинге
всего 256 МБ. С папкой PNG обе проблемы исчезают: читается только то, что
реально понадобилось.

Запускать на маке, результат коммитить:
    python tools/export_apple_emoji.py

Варианты тона кожи (.1-.5) пропускаются — это половина веса, а подстановка
жёлтого варианта по смыслу не меняет ничего.
"""

import re
import sys
from pathlib import Path

SRC = "/System/Library/Fonts/Apple Color Emoji.ttc"
OUT = Path(__file__).parent.parent / "fonts" / "emoji_apple"
STRIKE = 160
SKIP_TONES = True


def main() -> int:
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        print("нужен fonttools: pip install fonttools")
        return 1
    if not Path(SRC).exists():
        print(f"не нашёл {SRC} — скрипт рассчитан на macOS")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    font = TTFont(SRC, fontNumber=0, lazy=True)
    strike = font["sbix"].strikes[STRIKE]

    written = skipped = 0
    total = 0
    for name, g in strike.glyphs.items():
        data = g.imageData
        if not data:
            continue
        if SKIP_TONES and re.search(r"\.[1-5](\.|$)", name):
            skipped += 1
            continue
        (OUT / f"{name}.png").write_bytes(data)
        written += 1
        total += len(data)

    print(f"записано {written} файлов ({total / 1024 / 1024:.1f} МБ) в {OUT}")
    print(f"пропущено вариантов тона кожи: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
