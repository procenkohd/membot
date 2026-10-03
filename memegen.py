"""
Рисует текст поверх присланного фото. Два стиля, между которыми бот сам
рандомно выбирает: классический мем (Impact-стиль, верх/низ) и
демотиватор (чёрная рамка, курсивная подпись снизу).
"""

from io import BytesIO
import logging
from pathlib import Path
from typing import List, Optional
import random

from PIL import Image, ImageDraw, ImageFont

from smart_layout import Rect, VisualAnalysis, analyze_image

logger = logging.getLogger(__name__)

FONTS_DIR = Path(__file__).parent / "fonts"

# Настоящий Impact не поддерживает кириллицу, поэтому для классического
# мема есть выбор из шрифтов с кириллицей — три жирных капса (Oswald,
# Roboto Condensed, Rubik), какой выпадет, решает рандом (если не выбран
# явно, см. font_id в make_classic_meme). Рукописные отсюда выпилены:
# сначала Marck Script, потом Pacifico — капс с обводкой у них слипается
# в нечитаемую кашу, а текст в базе сплошь капсом.
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
# «Свой мем» намеренно остаётся простым и предсказуемым: знакомые три
# шрифта, выбранные пользователем, и фиксированные верх/низ. Новые стили
# участвуют только в случайной умной выдаче.
CUSTOM_FONT_IDS = ("oswald", "roboto_condensed", "rubik")
CUSTOM_FONT_CHOICES = [FONT_CHOICES_BY_ID[font_id] for font_id in CUSTOM_FONT_IDS]

DEMOTIVATOR_FONT_PATH = FONTS_DIR / "PTSerif-Italic.ttf"
DEMOTIVATOR_CHANCE = 0.2  # ~1 мем из 5 выходит демотиватором
SMART_LAYOUT_CHANCE = 0.65  # остальные классические мемы сохраняют старые верх/низ

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
                   max_block_height: int, margin: int = 14) -> None:
    if not text:
        return

    display_text = text.upper() if font_choice["upper"] else text
    spacing = 6

    # Уменьшаем блок и по высоте, и по ширине. Раньше одиночное длинное
    # слово не переносилось, проверялась только высота — из-за этого оно
    # могло вылезти за левую и правую границы изображения.
    working_font = font
    while True:
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
        if (total_height <= max_block_height and fits_width) or emergency_wrap:
            break
        new_size = max(14, int(working_font.size * 0.85))
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


ACCENT_COLORS = (
    "#FFE342",  # жёлтый — классический панч
    "#65E5FF",  # голубой
    "#FF79B9",  # розовый
    "#A8FF60",  # кислотно-зелёный
)


def _candidate_zones(image_size: tuple[int, int], allow_side: bool) -> list[tuple[Rect, bool]]:
    """Возвращает зоны-кандидаты: пять широких и, для короткого текста,
    шесть боковых. bool отмечает более тесную боковую зону."""
    width, height = image_size
    margin = max(12, int(width * 0.025))
    full_h = max(90, int(height * 0.27))
    full_w = width - 2 * margin
    full_y = (
        margin,
        int(height * 0.17),
        int(height * 0.36),
        int(height * 0.55),
        height - full_h - margin,
    )
    zones = [((margin, y, full_w, full_h), False) for y in full_y]

    if allow_side:
        gap = max(12, int(width * 0.025))
        side_w = (width - 2 * margin - gap) // 2
        side_h = max(110, int(height * 0.34))
        for y in (margin, int(height * 0.32), height - side_h - margin):
            zones.append(((margin, y, side_w, side_h), True))
            zones.append(((margin + side_w + gap, y, side_w, side_h), True))
    return zones


def _layout_in_zone(draw: ImageDraw.ImageDraw, text: str, font_choice: dict,
                    base_size: int, stroke_width: int, zone: Rect,
                    side_zone: bool) -> Optional[dict]:
    x, y, width, height = zone
    display_text = text.upper() if font_choice["upper"] else text
    size = int(base_size * (0.88 if side_zone else 1.0))
    spacing = max(4, size // 14)

    while size >= 14:
        working_font = _load_font(size, font_choice)
        lines = _wrap_text(
            draw, display_text, working_font, width - stroke_width * 2,
            break_long_words=size <= 14,
        )
        boxes = [draw.textbbox((0, 0), line, font=working_font,
                               stroke_width=stroke_width) for line in lines]
        line_widths = [box[2] - box[0] for box in boxes]
        line_heights = [box[3] - box[1] for box in boxes]
        total_height = sum(line_heights) + spacing * max(0, len(lines) - 1)
        max_line_width = max(line_widths, default=0)
        if total_height <= height and max_line_width <= width:
            actual_x = x + (width - max_line_width) // 2
            actual_y = y + (height - total_height) // 2
            return {
                "zone": zone,
                "rect": (actual_x, actual_y, max_line_width, total_height),
                "font": working_font,
                "lines": lines,
                "line_heights": line_heights,
                "spacing": spacing,
                "stroke_width": stroke_width,
                "side": side_zone,
            }
        size = max(13, int(size * 0.88))
    return None


def _caption_candidates(draw: ImageDraw.ImageDraw, text: str, font_choice: dict,
                        base_size: int, stroke_width: int,
                        image_size: tuple[int, int]) -> list[dict]:
    allow_side = len(text) <= 54
    candidates = []
    for zone, side_zone in _candidate_zones(image_size, allow_side):
        layout = _layout_in_zone(
            draw, text, font_choice, base_size, stroke_width, zone, side_zone)
        if layout:
            candidates.append(layout)
    return candidates


def _position_bias(layout: dict, image_height: int, role: str) -> float:
    _, y, _, h = layout["rect"]
    center = (y + h / 2) / image_height
    # Узкая боковая зона обычно означает, что на фото действительно нашёлся
    # свободный фон. Небольшой бонус компенсирует лишнюю строку переноса.
    side_bonus = -0.08 if layout["side"] else 0.0
    if role == "setup":
        return center * 0.35 + side_bonus
    if role == "punch":
        return (1.0 - center) * 0.35 + side_bonus
    return abs(0.5 - center) * 0.12 + side_bonus


def _choose_smart_layouts(analysis: VisualAnalysis, draw: ImageDraw.ImageDraw,
                          top_text: str, bottom_text: str, font_choice: dict,
                          base_size: int, stroke_width: int) -> list[tuple[dict, str]]:
    width, height = analysis.image_size
    if top_text and bottom_text:
        setups = _caption_candidates(
            draw, top_text, font_choice, base_size, stroke_width, (width, height))
        punches = _caption_candidates(
            draw, bottom_text, font_choice, base_size, stroke_width, (width, height))
        pairs = []
        for setup in setups:
            sx, sy, sw, sh = setup["rect"]
            setup_center = sy + sh / 2
            for punch in punches:
                px, py, pw, ph = punch["rect"]
                punch_center = py + ph / 2
                # Сохраняем естественный порядок чтения: завязка выше панча.
                if punch_center <= setup_center + height * 0.06:
                    continue
                score = (
                    analysis.score(setup["rect"])
                    + _position_bias(setup, height, "setup")
                    + analysis.score(punch["rect"], occupied=(setup["rect"],))
                    + _position_bias(punch, height, "punch")
                )
                pairs.append((score, setup, punch))
        if pairs:
            _, setup, punch = min(pairs, key=lambda item: item[0])
            return [(setup, "white"), (punch, random.choice(ACCENT_COLORS))]

    text = top_text or bottom_text
    if not text:
        return []
    candidates = _caption_candidates(
        draw, text, font_choice, base_size, stroke_width, (width, height))
    if not candidates:
        return []
    chosen = min(
        candidates,
        key=lambda layout: analysis.score(layout["rect"])
        + _position_bias(layout, height, "single"),
    )
    return [(chosen, "white")]


def _draw_smart_layout(draw: ImageDraw.ImageDraw, layouts: list[tuple[dict, str]]) -> None:
    for layout, color in layouts:
        zone_x, _, zone_w, _ = layout["zone"]
        _, y, _, _ = layout["rect"]
        for line, line_height in zip(layout["lines"], layout["line_heights"]):
            box = draw.textbbox(
                (0, 0), line, font=layout["font"],
                stroke_width=layout["stroke_width"],
            )
            line_width = box[2] - box[0]
            x = zone_x + (zone_w - line_width) / 2 - box[0]
            draw.text(
                (x, y - box[1]), line, font=layout["font"], fill=color,
                stroke_width=layout["stroke_width"], stroke_fill="black",
            )
            y += line_height + layout["spacing"]


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
                       smart_layout: bool = False,
                       random_style: bool = False) -> BytesIO:
    """Классический мем. font_choice можно передать явно (см.
    FONT_CHOICES_BY_ID) — иначе шрифт выбирается рандомно. Умная раскладка
    включается только для случайных мемов; «Свой мем» оставляет верх/низ."""
    img = Image.open(BytesIO(image_bytes)).convert("RGB")

    # немного уменьшим слишком большие фото
    if max(img.size) > MAX_SIDE:
        ratio = MAX_SIDE / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))

    # --- визуальный рандом №1: расположение одноблочной фразы ---
    # если фраза не разбита через | на верх/низ:
    #   - длинная (LONG_PHRASE_CHARS+) всегда режется на верх/низ - одним
    #     блоком она бы просто ужалась в мелкий текст в одном углу
    #   - короткая - три равновероятных исхода для разнообразия:
    #     остаётся внизу / переезжает одним блоком наверх / режется пополам
    if top_text and not bottom_text:
        top_text, bottom_text = "", top_text

    if bottom_text and not top_text:
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

    draw = ImageDraw.Draw(img)

    # --- визуальный рандом №2: шрифт, размер и толщина обводки гуляют ---
    font_choice = font_choice or random.choice(
        FONT_CHOICES if random_style else CUSTOM_FONT_CHOICES)
    base_font_size = _fit_font_size(img.width)
    font_size = int(base_font_size * random.uniform(0.88, 1.12))
    font = _load_font(font_size, font_choice)
    stroke_width = max(2, font_size // 14) + random.choice([-1, 0, 0, 1])
    stroke_width = max(2, stroke_width)
    max_text_width = img.width - 24

    # --- визуальный рандом №3: отступы от краёв немного гуляют ---
    top_margin = random.randint(8, 22)
    bottom_margin = random.randint(12, 26)

    smart_layouts = []
    if smart_layout:
        try:
            analysis = analyze_image(img)
            smart_layouts = _choose_smart_layouts(
                analysis, draw, top_text, bottom_text, font_choice,
                font_size, stroke_width,
            )
        except Exception:
            # Анализ — улучшение, а не критическая зависимость. Битое фото,
            # сбой каскада или нехватка памяти не должны ломать выдачу мема.
            logger.exception("Smart meme layout failed; using fixed top/bottom layout")
            smart_layouts = []

    if smart_layouts:
        _draw_smart_layout(draw, smart_layouts)
    else:
        max_block_height = int(img.height * 0.32)
        _draw_caption(draw, top_text, font, font_choice, img.size, "top", max_text_width,
                      stroke_width, max_block_height, top_margin)
        _draw_caption(draw, bottom_text, font, font_choice, img.size, "bottom", max_text_width,
                      stroke_width, max_block_height, bottom_margin)

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

    use_smart_layout = random.random() < SMART_LAYOUT_CHANCE
    return make_classic_meme(
        image_bytes, top_text, bottom_text,
        smart_layout=use_smart_layout,
        random_style=True,
    )
