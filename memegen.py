"""
Рисует текст поверх присланного фото. Два стиля, между которыми бот сам
рандомно выбирает: классический мем (Impact-стиль, верх/низ) и
демотиватор (чёрная рамка, курсивная подпись снизу).
"""

from io import BytesIO
from pathlib import Path
from typing import List, Optional
import random

from PIL import Image, ImageDraw, ImageFont

FONTS_DIR = Path(__file__).parent / "fonts"

# Настоящий Impact не поддерживает кириллицу, поэтому для классического
# мема есть выбор из шрифтов с кириллицей — три жирных капса (Oswald,
# Roboto Condensed, Rubik) и рукописный Pacifico — какой выпадет, решает
# рандом (если не выбран явно, см. font_id в make_classic_meme). Marck
# Script убрали: с обводкой был нечитаемым.
FONT_CHOICES_BY_ID = {
    "oswald": {"path": FONTS_DIR / "Oswald-Variable.ttf", "weight": 700, "upper": True, "label": "Oswald (капс)"},
    "roboto_condensed": {
        "path": FONTS_DIR / "RobotoCondensed-Variable.ttf", "weight": 800, "upper": True,
        "label": "Roboto Condensed (капс)",
    },
    "rubik": {"path": FONTS_DIR / "Rubik-Variable.ttf", "weight": 800, "upper": True, "label": "Rubik (капс)"},
    "pacifico": {
        "path": FONTS_DIR / "Pacifico-Regular.ttf", "weight": None, "upper": False,
        "label": "Pacifico (рукописный)",
    },
}

FONT_CHOICES = list(FONT_CHOICES_BY_ID.values())

DEMOTIVATOR_FONT_PATH = FONTS_DIR / "PTSerif-Italic.ttf"
DEMOTIVATOR_CHANCE = 0.2  # ~1 мем из 5 выходит демотиватором

MAX_SIDE = 1280  # чтобы не рожать гигантские файлы


def _load_font(size: int, font_choice: dict) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(font_choice["path"]), size)
    if font_choice["weight"] is not None:
        try:
            font.set_variation_by_axes([font_choice["weight"]])
        except Exception:
            pass  # если вариативность недоступна — используем дефолтный вес
    return font


def _fit_font_size(image_width: int) -> int:
    return max(24, image_width // 10)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    words = text.split()
    if not words:
        return []
    lines = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        w = draw.textlength(trial, font=font)
        if w <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _measure_block(draw: ImageDraw.ImageDraw, lines, font, stroke_width) -> int:
    spacing = 6
    line_heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        line_heights.append(bbox[3] - bbox[1])
    return sum(line_heights) + spacing * max(0, len(lines) - 1)


def _draw_caption(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
                   font_choice: dict, image_size, y_anchor: str, max_width: int, stroke_width: int,
                   max_block_height: int, margin: int = 14) -> None:
    if not text:
        return

    display_text = text.upper() if font_choice["upper"] else text
    lines = _wrap_text(draw, display_text, font, max_width)
    spacing = 6

    # если блок текста не влезает по высоте — на лету уменьшаем шрифт
    working_font = font
    while True:
        line_heights = [
            draw.textbbox((0, 0), line, font=working_font, stroke_width=stroke_width)[3]
            - draw.textbbox((0, 0), line, font=working_font, stroke_width=stroke_width)[1]
            for line in lines
        ]
        total_height = sum(line_heights) + spacing * max(0, len(lines) - 1)
        if total_height <= max_block_height or working_font.size <= 14:
            break
        new_size = max(14, int(working_font.size * 0.85))
        working_font = _load_font(new_size, font_choice)
        lines = _wrap_text(draw, display_text, working_font, max_width)

    img_w, img_h = image_size
    if y_anchor == "top":
        y = margin
    else:
        y = img_h - total_height - margin

    for i, line in enumerate(lines):
        w = draw.textlength(line, font=working_font)
        x = (img_w - w) / 2
        draw.text(
            (x, y),
            line,
            font=working_font,
            fill="white",
            stroke_width=stroke_width,
            stroke_fill="black",
        )
        y += line_heights[i] + spacing


def make_classic_meme(image_bytes: bytes, top_text: str, bottom_text: str,
                       font_choice: Optional[dict] = None) -> BytesIO:
    """Классический мем. font_choice можно передать явно (см.
    FONT_CHOICES_BY_ID) — иначе шрифт выбирается рандомно, как раньше."""
    img = Image.open(BytesIO(image_bytes)).convert("RGB")

    # немного уменьшим слишком большие фото
    if max(img.size) > MAX_SIDE:
        ratio = MAX_SIDE / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))

    # --- визуальный рандом №1: расположение одноблочной фразы ---
    # если фраза не разбита через | на верх/низ, у неё три равновероятных
    # исхода вместо вечного "всегда внизу":
    #   - остаётся одним блоком внизу
    #   - переезжает одним блоком наверх
    #   - режется примерно пополам по словам: половина наверх, половина вниз
    if top_text and not bottom_text:
        top_text, bottom_text = "", top_text

    if bottom_text and not top_text:
        roll = random.random()
        if roll < 0.35:
            top_text, bottom_text = bottom_text, ""
        elif roll < 0.55:
            words = bottom_text.split()
            if len(words) >= 4:
                mid = len(words) // 2
                top_text = " ".join(words[:mid])
                bottom_text = " ".join(words[mid:])

    draw = ImageDraw.Draw(img)

    # --- визуальный рандом №2: шрифт, размер и толщина обводки гуляют ---
    font_choice = font_choice or random.choice(FONT_CHOICES)
    base_font_size = _fit_font_size(img.width)
    font_size = int(base_font_size * random.uniform(0.88, 1.12))
    font = _load_font(font_size, font_choice)
    stroke_width = max(2, font_size // 14) + random.choice([-1, 0, 0, 1])
    stroke_width = max(2, stroke_width)
    max_text_width = img.width - 24

    # --- визуальный рандом №3: отступы от краёв немного гуляют ---
    top_margin = random.randint(8, 22)
    bottom_margin = random.randint(12, 26)

    max_block_height = int(img.height * 0.32)
    _draw_caption(draw, top_text, font, font_choice, img.size, "top", max_text_width, stroke_width,
                  max_block_height, top_margin)
    _draw_caption(draw, bottom_text, font, font_choice, img.size, "bottom", max_text_width, stroke_width,
                  max_block_height, bottom_margin)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    buf.name = "meme.jpg"
    return buf


def make_demotivator(image_bytes: bytes, caption: str, subtitle: str = "") -> BytesIO:
    """Классический демотиватор: чёрное поле, тонкая белая рамка вокруг
    фото, курсивная подпись снизу (+ необязательная мелкая подстрочная
    строка)."""
    img = Image.open(BytesIO(image_bytes)).convert("RGB")

    max_photo_side = 900
    if max(img.size) > max_photo_side:
        ratio = max_photo_side / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))

    border_black = 50       # чёрное поле вокруг белой рамки
    white_gap = 8            # отступ между фото и белой линией
    white_line = 2            # толщина белой линии

    framed_w = img.width + 2 * (white_gap + white_line)
    framed_h = img.height + 2 * (white_gap + white_line)
    canvas_w = framed_w + 2 * border_black

    caption_font_size = max(26, canvas_w // 16)
    subtitle_font_size = max(16, int(caption_font_size * 0.5))
    caption_font = ImageFont.truetype(str(DEMOTIVATOR_FONT_PATH), caption_font_size)
    subtitle_font = ImageFont.truetype(str(DEMOTIVATOR_FONT_PATH), subtitle_font_size)

    scratch = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    max_text_width = canvas_w - 80

    caption_lines = _wrap_text(scratch, caption, caption_font, max_text_width) if caption else []
    subtitle_lines = _wrap_text(scratch, subtitle, subtitle_font, max_text_width) if subtitle else []

    line_gap = 10

    def block_height(lines, font):
        if not lines:
            return 0
        h = sum(scratch.textbbox((0, 0), l, font=font)[3] - scratch.textbbox((0, 0), l, font=font)[1] for l in lines)
        return h + line_gap * (len(lines) - 1)

    caption_h = block_height(caption_lines, caption_font)
    subtitle_h = block_height(subtitle_lines, subtitle_font)

    text_area_h = 40 + caption_h + (18 if subtitle_lines else 0) + subtitle_h + 40
    if not caption_lines and not subtitle_lines:
        text_area_h = 30  # совсем без подписи — минимальный отступ снизу

    canvas_h = border_black + framed_h + text_area_h
    canvas = Image.new("RGB", (canvas_w, canvas_h), "black")
    draw = ImageDraw.Draw(canvas)

    frame_x0 = border_black
    frame_y0 = border_black
    frame_x1 = frame_x0 + framed_w
    frame_y1 = frame_y0 + framed_h
    draw.rectangle([frame_x0, frame_y0, frame_x1, frame_y1], outline="white", width=white_line)

    photo_x = frame_x0 + white_line + white_gap
    photo_y = frame_y0 + white_line + white_gap
    canvas.paste(img, (photo_x, photo_y))

    y = frame_y1 + 40
    for line in caption_lines:
        bbox = draw.textbbox((0, 0), line, font=caption_font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        draw.text(((canvas_w - w) / 2, y), line, font=caption_font, fill="white")
        y += h + line_gap

    if subtitle_lines:
        y += 18
        for line in subtitle_lines:
            bbox = draw.textbbox((0, 0), line, font=subtitle_font)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            draw.text(((canvas_w - w) / 2, y), line, font=subtitle_font, fill="white")
            y += h + line_gap

    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    buf.name = "demotivator.jpg"
    return buf


def make_meme(image_bytes: bytes, top_text: str, bottom_text: str) -> BytesIO:
    """Точка входа, которой пользуется бот. Сама рандомно решает, какой
    стиль выдать — классический мем или демотиватор — так что вызывающему
    коду (bot.py) вообще ничего менять не нужно."""
    if random.random() < DEMOTIVATOR_CHANCE:
        if top_text and bottom_text:
            caption, subtitle = top_text, bottom_text
        else:
            caption, subtitle = (top_text or bottom_text), ""
        return make_demotivator(image_bytes, caption, subtitle)

    return make_classic_meme(image_bytes, top_text, bottom_text)
