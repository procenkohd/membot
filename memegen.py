"""
Рисует текст поверх присланного фото. Два стиля, между которыми бот сам
рандомно выбирает: классический мем (Impact-стиль, верх/низ) и
демотиватор (чёрная рамка, курсивная подпись снизу).
"""

from io import BytesIO
from pathlib import Path
from typing import List, Optional
import random

from PIL import Image, ImageDraw, ImageFont, ImageOps

FONTS_DIR = Path(__file__).parent / "fonts"

# Старые идентификаторы остаются для совместимости импортов и тестов.
# Новые классические мемы всегда используют Oswald Bold с кириллицей.
FONT_CHOICES_BY_ID = {
    "oswald": {"path": FONTS_DIR / "Oswald-Variable.ttf", "weight": 700, "upper": True, "label": "Oswald (капс)"},
    "roboto_condensed": {
        "path": FONTS_DIR / "RobotoCondensed-Variable.ttf", "weight": 800, "upper": True,
        "label": "Roboto Condensed (капс)",
    },
    "rubik": {"path": FONTS_DIR / "Rubik-Variable.ttf", "weight": 800, "upper": True, "label": "Rubik (капс)"},
    "inter": {"path": FONTS_DIR / "InterVariable.ttf", "weight": 800, "upper": True, "label": "Inter (капс)"},
    "pt_serif": {
        "path": FONTS_DIR / "PTSerif-Italic.ttf", "weight": None, "upper": False,
        "label": "PT Serif (курсив)",
    },
}

FONT_CHOICES = list(FONT_CHOICES_BY_ID.values())
# В интерфейсе выбора шрифта больше нет.
DEFAULT_FONT_ID = "oswald"
DEFAULT_FONT = FONT_CHOICES_BY_ID[DEFAULT_FONT_ID]
CUSTOM_FONT_IDS = (DEFAULT_FONT_ID,)
CUSTOM_FONT_CHOICES = [FONT_CHOICES_BY_ID[font_id] for font_id in CUSTOM_FONT_IDS]

DEMOTIVATOR_FONT_PATH = FONTS_DIR / "PTSerif-Italic.ttf"
DEMOTIVATOR_CHANCE = 0.2  # ~1 мем из 5 выходит демотиватором

MAX_SIDE = 1280  # чтобы не рожать гигантские файлы


def _load_font(size: int, font_choice: dict) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(font_choice["path"]), size)
    if font_choice["weight"] is not None:
        try:
            axes = font.get_variation_axes()
            values = [axis["default"] for axis in axes]
            for index, axis in enumerate(axes):
                axis_name = axis.get("name", b"")
                if isinstance(axis_name, bytes):
                    axis_name = axis_name.decode("ascii", errors="ignore")
                if axis_name.lower() == "weight":
                    values[index] = font_choice["weight"]
            font.set_variation_by_axes(values)
        except Exception:
            pass  # если вариативность недоступна — используем дефолтный вес
    return font


def _fit_font_size(image_width: int) -> int:
    return max(24, image_width // 10)


def _split_long_word(draw: ImageDraw.ImageDraw, word: str,
                     font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    """Аварийно режет слитный текст, если даже минимальный шрифт шире кадра."""
    chunks: List[str] = []
    current = ""
    for char in word:
        trial = current + char
        if current and draw.textlength(trial, font=font) > max_width:
            chunks.append(current)
            current = char
        else:
            current = trial
    if current:
        chunks.append(current)
    return chunks or [word]


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               max_width: int, break_long_words: bool = False) -> List[str]:
    words = text.split()
    if not words:
        return []
    if break_long_words:
        words = [
            chunk
            for word in words
            for chunk in (
                _split_long_word(draw, word, font, max_width)
                if draw.textlength(word, font=font) > max_width
                else [word]
            )
        ]
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
                   max_block_height: int, margin: int = 14) -> int:
    if not text.strip():
        return font.size

    display_text = text.upper() if font_choice["upper"] else text
    spacing = 6

    # Уменьшаем блок и по высоте, и по ширине. Раньше одиночное длинное
    # слово не переносилось, проверялась только высота — из-за этого оно
    # могло вылезти за левую и правую границы изображения.
    working_font = font
    while True:
        spacing = min(6, working_font.size // 4)
        stroke_width = min(stroke_width, working_font.size // 10)
        emergency_wrap = working_font.size <= 14
        lines = _wrap_text(
            draw, display_text, working_font, max_width,
            break_long_words=emergency_wrap,
        )
        boxes = [
            draw.textbbox((0, 0), line, font=working_font, stroke_width=stroke_width)
            for line in lines
        ]
        line_heights = [box[3] - box[1] for box in boxes]
        line_widths = [box[2] - box[0] for box in boxes]
        total_height = sum(line_heights) + spacing * max(0, len(lines) - 1)
        fits_width = max(line_widths, default=0) <= max_width
        if total_height <= max_block_height and fits_width:
            break
        if working_font.size <= 1:
            raise ValueError("Image too small to fit caption")
        new_size = max(1, int(working_font.size * 0.85))
        working_font = _load_font(new_size, font_choice)

    img_w, img_h = image_size
    if y_anchor == "top":
        y = margin
    else:
        y = img_h - total_height - margin

    for i, (line, box) in enumerate(zip(lines, boxes)):
        line_width = box[2] - box[0]
        x = (img_w - line_width) / 2 - box[0]
        draw.text(
            (x, y - box[1]),
            line,
            font=working_font,
            fill="white",
            stroke_width=stroke_width,
            stroke_fill="black",
        )
        y += line_heights[i] + spacing
    return working_font.size


def _prepare_photo(image_bytes: bytes, max_side: int, *, pad: bool = True) -> Image.Image:
    with Image.open(BytesIO(image_bytes)) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    if not pad:
        return image
    # Very narrow photos need a usable canvas; never stretch or crop the photo.
    return ImageOps.expand(image, (
        max(0, (320 - image.width) // 2), max(0, (160 - image.height) // 2),
        max(0, (321 - image.width) // 2), max(0, (161 - image.height) // 2),
    ), fill="black")


LONG_PHRASE_CHARS = 50  # длиннее - одним блоком выглядит как стена мелкого текста в одном углу

_SPLIT_CONJUNCTIONS = {
    "а", "но", "и", "или", "если", "когда", "чтобы", "потому", "зато",
    "либо", "хотя", "пока", "раз", "ведь", "что", "как", "чем",
    # мемные слова-призывы: почти всегда начинают вторую половину ("... репост если...")
    "репост", "лайк", "шер", "дизлайк", "донат", "подписка", "плюс", "минус",
}

# предлоги и частицы, на которых нельзя обрывать верхнюю строку - иначе "в"
# повисает без своего дополнения ("я прибыл в|своём времени" читается коряво)
_NO_DANGLE_AT_END = {
    "в", "во", "на", "с", "со", "к", "ко", "у", "о", "об", "от", "до", "из",
    "по", "под", "над", "за", "при", "для", "без", "про", "через", "между",
    "перед", "не", "ни", "же", "бы", "ль", "и", "а", "но", "что", "как", "то",
}


def _split_phrase_for_layout(text: str) -> Optional[tuple]:
    """Делит нераздельную фразу на верх/низ по словам.
    Приоритеты (по убыванию надёжности):
    1. запятая рядом с серединой - в русском это почти всегда граница
       смысловых частей ("это не X, это Y"), самый надёжный маркер;
    2. иначе - точка, где символьная длина половин наиболее сбалансирована
       (поровну слов не значит поровну текста - слова разной длины), с
       поправкой на ближайший союз/связку, если он рядом (±2 слова);
    3. в любом случае не даём верхней строке оборваться на предлоге/частице
       без своего дополнения - сдвигаем точку разреза на следующее слово.
    None, если слов меньше 4 (резать особо нечего)."""
    words = text.split()
    if len(words) < 4:
        return None

    cum = [0]
    for w in words:
        cum.append(cum[-1] + len(w) + 1)
    total = cum[-1] - 1

    def diff_at(i: int) -> int:
        left_len = cum[i] - 1
        return abs(left_len - (total - left_len))

    comma_positions = [i for i in range(1, len(words)) if words[i - 1].endswith(",")]
    if comma_positions:
        best_i = min(comma_positions, key=diff_at)
    else:
        best_i = min(range(1, len(words)), key=diff_at)
        for i in range(max(1, best_i - 2), min(len(words), best_i + 3)):
            if words[i].lower().strip(",.!?…") in _SPLIT_CONJUNCTIONS:
                best_i = i
                break

    for _ in range(3):
        if best_i >= len(words) - 1:
            break
        last_word = words[best_i - 1].lower().strip(",.!?…")
        if last_word not in _NO_DANGLE_AT_END:
            break
        best_i += 1

    return " ".join(words[:best_i]), " ".join(words[best_i:])


def make_classic_meme(image_bytes: bytes, top_text: str, bottom_text: str,
                       font_choice: Optional[dict] = None,
                       random_style: bool = False) -> BytesIO:
    """Oswald Bold, белая подпись с чёрной обводкой, верх/низ.

    font_choice принят для совместимости, но больше не меняет шрифт.
    random_style разрешает перестановку только автоматически выбранной фразы.
    """
    img = _prepare_photo(image_bytes, MAX_SIDE, pad=False)

    # --- визуальный рандом №1: расположение одноблочной фразы ---
    # если фраза не разбита через | на верх/низ:
    #   - длинная (LONG_PHRASE_CHARS+) всегда режется на верх/низ - одним
    #     блоком она бы просто ужалась в мелкий текст в одном углу
    #   - короткая - три равновероятных исхода для разнообразия:
    #     остаётся внизу / переезжает одним блоком наверх / режется пополам
    if random_style and top_text and not bottom_text:
        top_text, bottom_text = "", top_text

    if random_style and bottom_text and not top_text:
        if len(bottom_text) >= LONG_PHRASE_CHARS:
            split = _split_phrase_for_layout(bottom_text)
            if split:
                top_text, bottom_text = split
        else:
            roll = random.random()
            if roll < 0.35:
                top_text, bottom_text = bottom_text, ""
            elif roll < 0.55:
                split = _split_phrase_for_layout(bottom_text)
                if split:
                    top_text, bottom_text = split

    # Fit captions on a transparent layer, never on an added black panel.
    # A minimum text grid also handles tiny/extremely narrow inputs without
    # stretching, cropping or adding borders to the photo itself.
    layer = Image.new("RGBA", (max(320, img.width), max(160, img.height)))
    size = _fit_font_size(layer.width)
    for position, text in (("top", top_text), ("bottom", bottom_text)):
        _draw_caption(
            ImageDraw.Draw(layer), text, _load_font(size, DEFAULT_FONT), DEFAULT_FONT,
            layer.size, position, layer.width - 28, max(2, size // 14), int(layer.height * .32), 14)
    if layer.size != img.size:
        layer = layer.resize(img.size, Image.Resampling.LANCZOS)
    img.paste(layer, (0, 0), layer)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    buf.name = "meme.jpg"
    buf.layout = {"fmt": "meme", "top": top_text, "bottom": bottom_text}
    buf.safety = {"bands": (), "photo_box": (0, 0, img.width, img.height)}
    return buf


def make_demotivator(image_bytes: bytes, caption: str, subtitle: str = "") -> BytesIO:
    """Классический демотиватор: чёрное поле, тонкая белая рамка вокруг
    фото, курсивная подпись снизу (+ необязательная мелкая подстрочная
    строка)."""
    img = _prepare_photo(image_bytes, 900)

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

    caption_lines = _wrap_text(scratch, caption, caption_font, max_text_width, True) if caption else []
    subtitle_lines = _wrap_text(scratch, subtitle, subtitle_font, max_text_width, True) if subtitle else []

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
        draw.text(((canvas_w - w) / 2 - bbox[0], y - bbox[1]), line, font=caption_font, fill="white")
        y += h + line_gap

    if subtitle_lines:
        y += 18
        for line in subtitle_lines:
            bbox = draw.textbbox((0, 0), line, font=subtitle_font)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            draw.text(((canvas_w - w) / 2 - bbox[0], y - bbox[1]), line, font=subtitle_font, fill="white")
            y += h + line_gap

    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    buf.name = "demotivator.jpg"
    buf.layout = {"fmt": "demotivator", "top": caption, "bottom": subtitle}
    return buf


VERY_LONG_UNSPLIT_CHARS = 110  # длиннее и без | - разрежь как угодно, на классике всё равно "стена текста"
DEMOTIVATOR_ONLY_PREFIX = "~"  # "~фраза" в phrases.txt = у фразы нет формы верх/низ, только демотиватор


def make_meme(image_bytes: bytes, top_text: str, bottom_text: str) -> BytesIO:
    """Точка входа, которой пользуется бот. Сама рандомно решает, какой
    стиль выдать — классический мем или демотиватор — так что вызывающему
    коду (bot.py) вообще ничего менять не нужно."""
    # ~ в начале фразы - явная пометка "эту резать на верх/низ бессмысленно"
    forced_by_marker = False
    if top_text.startswith(DEMOTIVATOR_ONLY_PREFIX):
        top_text, forced_by_marker = top_text[1:].lstrip(), True
    elif bottom_text.startswith(DEMOTIVATOR_ONLY_PREFIX):
        bottom_text, forced_by_marker = bottom_text[1:].lstrip(), True

    # длинная нераздельная фраза - это цельная мысль без чёткой формы
    # "завязка/панчлайн", у классического мема (верх/низ, ужатый в 32%
    # высоты фото) под такое просто нет подходящей формы. Демотиватор для
    # длинных ироничных подписей жанрово как раз создан - подпись под
    # фото, без жёсткого лимита высоты.
    force_demotivator = forced_by_marker or (
        bottom_text and not top_text and len(bottom_text) >= VERY_LONG_UNSPLIT_CHARS
    )

    if force_demotivator or random.random() < DEMOTIVATOR_CHANCE:
        if top_text and bottom_text:
            caption, subtitle = top_text, bottom_text
        else:
            caption, subtitle = (top_text or bottom_text), ""
        return make_demotivator(image_bytes, caption, subtitle)

    return make_classic_meme(
        image_bytes, top_text, bottom_text,
        random_style=True,
    )
