"""Пересобирает assets/theme_preview.jpg — картинку выбора оформления.

Её показывает бот в сценарии переписки, и номера на ней обязаны совпадать с
номерами кнопок. И то и другое берётся из порядка THEMES, так что после
добавления темы достаточно запустить этот скрипт:

    python tools/make_theme_preview.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from PIL import Image, ImageDraw, ImageFont

from chatgen import (HEADER_H, HOME_H, INPUT_H, PX, STATUS_H, THEMES, Msg,
                     make_chat_screenshot)

OUT = Path(__file__).parent.parent / "assets" / "theme_preview.jpg"
COLS = 4
TILE_W = 250

SAMPLE = [
    Msg("го в кино", False, "19:02"),
    Msg("не, я дома сгнил 💀", True, "19:03"),
    Msg("ну ты и пельмень", False, "19:03", reaction="😁"),
    Msg("зато в тепле", True, "19:05"),
]


def tile(theme_key: str) -> Image.Image:
    """Шапка плюс низ чата: середина экрана на превью всё равно пустая."""
    im = Image.open(make_chat_screenshot(
        SAMPLE, theme=theme_key, contact_name="Аня", subtitle="был(а) недавно",
        unread=12, clock="19:05")).convert("RGB")
    head = im.crop((0, 0, im.width, PX(STATUS_H + HEADER_H)))
    body = im.crop((0, im.height - PX(HOME_H + INPUT_H) - PX(270),
                    im.width, im.height - PX(HOME_H + INPUT_H)))
    out = Image.new("RGB", (im.width, head.height + body.height))
    out.paste(head, (0, 0))
    out.paste(body, (0, head.height))
    return out


def main() -> None:
    keys = list(THEMES)
    tiles = [tile(k) for k in keys]
    w, h = tiles[0].size
    scale = TILE_W / w
    tw, th = int(w * scale), int(h * scale)
    pad, lab = 14, 34
    rows = (len(tiles) + COLS - 1) // COLS
    canvas = Image.new("RGB", (COLS * tw + (COLS + 1) * pad,
                               rows * (th + lab) + pad), (16, 17, 20))
    d = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(str(Path(__file__).parent.parent / "fonts" / "InterVariable.ttf"), 21)
    for i, (img, key) in enumerate(zip(tiles, keys)):
        label = f"{i + 1} — {THEMES[key].title}"
        x = pad + (i % COLS) * (tw + pad)
        y = pad + (i // COLS) * (th + lab)
        d.text((x + (tw - d.textlength(label, font=font)) / 2, y), label,
               font=font, fill=(238, 238, 242))
        canvas.paste(img.resize((tw, th), Image.LANCZOS), (x, y + lab - 6))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(OUT, quality=87)
    print(f"готово: {OUT} ({canvas.size[0]}x{canvas.size[1]}), тем {len(keys)}")


if __name__ == "__main__":
    main()
