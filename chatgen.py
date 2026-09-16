"""Рендер фейкового скриншота переписки в Telegram (iOS).

Отдельная от memegen.py история: там мем поверх чужого фото, тут экран
телефона рисуется с нуля. Всё считается в «пойнтах» айфона и умножается на
SCALE — так константы совпадают с теми, что видно в макетах iOS, а не с
пикселями конкретной модели.

Точка входа — make_chat_screenshot(). Ниже по файлу: темы, примитивы
(скруглённые прямоугольники с хвостиком, блюр), текст с эмодзи, раскладка.
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

import storage

BASE_DIR = Path(__file__).parent
FONTS_DIR = BASE_DIR / "fonts"

# Экран iPhone 15 Pro: 393x852 пойнта при плотности 3. Все константы ниже -
# в пойнтах, PX() переводит их в пиксели холста.
SCREEN_W_PT = 390
SCREEN_H_PT = 844
SCALE = 3


def PX(pt: float) -> int:
    return int(round(pt * SCALE))


# ---------- шрифты ----------

_FONT_CACHE: dict = {}


def font(size_pt: float, weight: int = 400) -> ImageFont.FreeTypeFont:
    """Inter вместо SF Pro: метрики почти те же, а лицензия свободная."""
    key = (round(size_pt * SCALE), weight)
    if key not in _FONT_CACHE:
        f = ImageFont.truetype(str(FONTS_DIR / "InterVariable.ttf"), key[0])
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
        _FONT_CACHE[key] = f
    return _FONT_CACHE[key]


# NotoColorEmoji - битмапный шрифт, Pillow открывает его только на кегле 109.
# Поэтому эмодзи рендерится в свой размер и уменьшается.
_EMOJI_FONT: Optional[ImageFont.FreeTypeFont] = None
_EMOJI_CACHE: dict = {}
EMOJI_NATIVE = 109


def _emoji_font() -> Optional[ImageFont.FreeTypeFont]:
    global _EMOJI_FONT
    if _EMOJI_FONT is None:
        path = FONTS_DIR / "NotoColorEmoji.ttf"
        if not path.exists():
            return None
        try:
            _EMOJI_FONT = ImageFont.truetype(str(path), EMOJI_NATIVE)
        except Exception:
            return None
    return _EMOJI_FONT


# Битмап в шрифте всего 136x128, поэтому крупные эмодзи (стикер, реакция на
# кружке) из него растягиваются в мыло. Для таких берём PNG 512x512 из набора
# noto-emoji: один раз скачали, дальше лежит в кэше рядом с остальным состоянием.
EMOJI_PNG_URL = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/512/{}.png"
EMOJI_PNG_MIN = 110          # ниже этого размера шрифт и так резкий
_EMOJI_PNG_MISS: set = set()


def _emoji_png_names(ch: str) -> list:
    """Имена файлов в noto-emoji: emoji_u<коды через _>. Селектор fe0f там
    обычно опущен, поэтому пробуем оба варианта, а затем без модификаторов."""
    cps = [ord(c) for c in ch]
    variants = [
        [c for c in cps if c != 0xFE0F],
        cps,
        [c for c in cps if c not in (0xFE0F, 0x200D) and not 0x1F3FB <= c <= 0x1F3FF],
    ]
    names, seen = [], set()
    for v in variants:
        if not v:
            continue
        name = "emoji_u" + "_".join(f"{c:x}" for c in v)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _emoji_png(ch: str) -> Optional[Image.Image]:
    cache_dir = storage.data_path("emoji_cache")
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    for name in _emoji_png_names(ch):
        if name in _EMOJI_PNG_MISS:
            continue
        path = Path(cache_dir) / f"{name}.png"
        if path.exists():
            try:
                return Image.open(path).convert("RGBA")
            except Exception:
                path.unlink(missing_ok=True)
        try:
            import urllib.request
            with urllib.request.urlopen(EMOJI_PNG_URL.format(name), timeout=6) as r:
                data = r.read()
            path.write_bytes(data)
            return Image.open(path).convert("RGBA")
        except Exception:
            _EMOJI_PNG_MISS.add(name)
    return None


def render_emoji(ch: str, px: int) -> Optional[Image.Image]:
    key = (ch, px)
    if key in _EMOJI_CACHE:
        return _EMOJI_CACHE[key]
    if px >= EMOJI_PNG_MIN:
        hi = _emoji_png(ch)
        if hi is not None:
            bb = hi.getbbox()          # в PNG свои поля, режем по содержимому,
            if bb:                     # иначе эмодзи выходит мельче шрифтового
                hi = hi.crop(bb)
            side = max(hi.size)
            sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            sq.paste(hi, ((side - hi.width) // 2, (side - hi.height) // 2), hi)
            out = sq.resize((px, px), Image.LANCZOS)
            _EMOJI_CACHE[key] = out
            return out
    f = _emoji_font()
    if f is None:
        return None
    try:
        tile = Image.new("RGBA", (EMOJI_NATIVE * 2, EMOJI_NATIVE * 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(tile)
        d.text((0, 0), ch, font=f, embedded_color=True)
        bbox = tile.getbbox()
        if not bbox:
            return None
        tile = tile.crop(bbox)
        # эмодзи должен быть квадратным, иначе в строке он «пляшет»
        side = max(tile.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(tile, ((side - tile.width) // 2, (side - tile.height) // 2))
        out = square.resize((px, px), Image.LANCZOS)
    except Exception:
        return None
    _EMOJI_CACHE[key] = out
    return out


def glyph(ch: str, px_size: int, color) -> Optional[Image.Image]:
    """Монохромная иконка из силуэта цветного эмодзи."""
    em = render_emoji(ch, px_size)
    if em is None:
        return None
    solid = Image.new("RGBA", em.size, tuple(color[:3]) + (255,))
    a = em.getchannel("A")
    if len(color) > 3 and color[3] < 255:
        a = a.point(lambda v: v * color[3] // 255)
    solid.putalpha(a)
    return solid


# Диапазоны эмодзи: пиктограммы, символы, флаги, стрелки-дингбаты.
_EMOJI_RE = re.compile(
    "([\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U00002BFF\U0000FE0F\U00002122\U000000A9\U000000AE]"
    "[\U0000FE0F\U0000200D\U0001F3FB-\U0001F3FF]*)+"
)


def split_runs(text: str) -> list:
    """Режет строку на куски «обычный текст» / «эмодзи» — рисуются они разными
    шрифтами, поэтому и меряются отдельно."""
    runs = []
    pos = 0
    for m in _EMOJI_RE.finditer(text):
        if m.start() > pos:
            runs.append(("text", text[pos:m.start()]))
        runs.append(("emoji", m.group()))
        pos = m.end()
    if pos < len(text):
        runs.append(("text", text[pos:]))
    return [r for r in runs if r[1]]


def measure_runs(runs: Sequence, f: ImageFont.FreeTypeFont, emoji_px: int) -> int:
    w = 0
    for kind, s in runs:
        if kind == "text":
            w += int(f.getlength(s))
        else:
            w += emoji_px * max(1, len(_EMOJI_RE.findall(s)) or 1)
    return w


def measure(text: str, f: ImageFont.FreeTypeFont, emoji_px: int) -> int:
    return measure_runs(split_runs(text), f, emoji_px)


def draw_runs(img: Image.Image, xy, text: str, f: ImageFont.FreeTypeFont,
              fill, emoji_px: int, line_h: int) -> None:
    """Рисует строку, подменяя шрифт на эмодзи-битмапы там, где надо."""
    x, y = xy
    d = ImageDraw.Draw(img)
    for kind, s in split_runs(text):
        if kind == "text":
            d.text((x, y), s, font=f, fill=fill)
            x += int(f.getlength(s))
        else:
            for token in _EMOJI_RE.findall(s) or [s]:
                em = render_emoji(token, emoji_px)
                if em is None:
                    d.text((x, y), token, font=f, fill=fill)
                    x += int(f.getlength(token))
                else:
                    img.alpha_composite(em, (int(x), int(y + (line_h - emoji_px) / 2)))
                    x += emoji_px


def wrap_text(text: str, f: ImageFont.FreeTypeFont, max_w: int, emoji_px: int) -> list:
    """Перенос по словам; \\n уважается, слишком длинное слово рвётся посимвольно."""
    lines = []
    for para in text.split("\n"):
        words = para.split(" ")
        cur = ""
        for word in words:
            probe = word if not cur else cur + " " + word
            if measure(probe, f, emoji_px) <= max_w or not cur:
                if measure(probe, f, emoji_px) > max_w and not cur:
                    # одно слово шире пузыря - режем по буквам
                    chunk = ""
                    for chunk_ch in word:
                        if measure(chunk + chunk_ch, f, emoji_px) > max_w and chunk:
                            lines.append(chunk)
                            chunk = chunk_ch
                        else:
                            chunk += chunk_ch
                    cur = chunk
                else:
                    cur = probe
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


# ---------- примитивы ----------

def overlay(canvas: Image.Image):
    """Прозрачный слой размером с холст + его Draw.

    Нужен потому, что ImageDraw не смешивает цвета с подложкой, а замещает их:
    линия с alpha=26, нарисованная прямо по холсту, станет не еле заметной, а
    полностью белой после сведения в RGB.
    """
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    return layer, ImageDraw.Draw(layer)


def _bezier(pts: Sequence, steps: int = 24) -> list:
    """Кубическая кривая по 4 точкам — ей рисуется хвостик пузыря."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append((
            u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
            u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3,
        ))
    return out


TAIL_W = 5.7   # насколько хвостик вылезает за грань пузыря, пойнты
TAIL_H = 8


def bubble_mask(w: int, h: int, r: int, tail: Optional[str]):
    """Альфа-маска пузыря; возвращает (маска, dx), где dx — отступ тела пузыря
    внутри маски. Маска шире самого пузыря, иначе хвостик обрезается по краю.
    Рисуется в двойном размере и уменьшается: без этого скругления выходят
    ступеньками, а это первое, что выдаёт подделку."""
    ss = 2
    tw = PX(TAIL_W) if tail else 0
    mw = w + tw
    dx = tw if tail == "left" else 0
    W, H = mw * ss, h * ss
    bx0, bx1 = dx * ss, (dx + w) * ss
    # Pillow ругается, если радиус больше половины стороны — а короткий пузырь
    # как раз такой
    R = max(0, int(min(r * ss, (H - 3) / 2, (bx1 - bx0 - 3) / 2)))
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    corners = (True, True, tail != "right", tail != "left")
    d.rounded_rectangle((bx0, 0, bx1 - 1, H - 1), radius=R, fill=255, corners=corners)
    if tail:
        tws, ths = tw * ss, PX(TAIL_H) * ss
        if tail == "right":
            path = [(bx1 - tws * 0.7, H)] + _bezier([
                (bx1, H), (bx1 + tws * 0.5, H), (bx1 + tws, H - ths * 0.25), (bx1 + tws, H - ths * 0.62),
            ]) + _bezier([
                (bx1 + tws, H - ths * 0.62), (bx1 + tws * 0.62, H - ths * 0.78), (bx1, H - ths * 0.72), (bx1, H - ths),
            ])
        else:
            path = [(bx0 + tws * 0.7, H)] + _bezier([
                (bx0, H), (bx0 - tws * 0.5, H), (bx0 - tws, H - ths * 0.25), (bx0 - tws, H - ths * 0.62),
            ]) + _bezier([
                (bx0 - tws, H - ths * 0.62), (bx0 - tws * 0.62, H - ths * 0.78), (bx0, H - ths * 0.72), (bx0, H - ths),
            ])
        d.polygon([(int(x), int(y)) for x, y in path], fill=255)
    return m.resize((mw, h), Image.LANCZOS), dx


def mesh_gradient(size, colors) -> Image.Image:
    """Фирменные телеграмовские обои-градиент: четыре цвета по углам,
    растянутые бикубиком. Ровно так они и устроены."""
    small = Image.new("RGB", (2, 2))
    small.putpixel((0, 0), colors[0])
    small.putpixel((1, 0), colors[1])
    small.putpixel((0, 1), colors[2])
    small.putpixel((1, 1), colors[3])
    return small.resize(size, Image.BICUBIC)


def _snowflake(d: ImageDraw.ImageDraw, cx: float, cy: float, rad: float, color, width: int) -> None:
    for i in range(6):
        a = math.pi / 3 * i
        x2, y2 = cx + rad * math.cos(a), cy + rad * math.sin(a)
        d.line((cx, cy, x2, y2), fill=color, width=width)
        for t in (0.55, 0.8):
            bx, by = cx + rad * t * math.cos(a), cy + rad * t * math.sin(a)
            for s in (-1, 1):
                a2 = a + s * math.pi / 4
                d.line((bx, by, bx + rad * 0.22 * math.cos(a2), by + rad * 0.22 * math.sin(a2)),
                       fill=color, width=width)


def snow_pattern(size, color) -> Image.Image:
    """Узор со снежинками поверх обоев. На референсе он плотный и разнокалиберный:
    крупные снежинки вперемешку с мелкими ромбами, поэтому одной сеткой не обойтись."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rnd = random.Random(20)
    w = max(1, PX(0.9))
    step = PX(84)
    for row, y in enumerate(range(-step, size[1] + step, step)):
        for x in range(-step, size[0] + step, step):
            ox = x + (step // 2 if row % 2 else 0) + rnd.randint(-PX(9), PX(9))
            oy = y + rnd.randint(-PX(9), PX(9))
            _snowflake(d, ox, oy, PX(rnd.choice((22, 28, 34))), color, w)
            # мелкий ромб в промежутке — он и создаёт ощущение плотного узора
            mx, my = ox + step // 2, oy + step // 2
            r = PX(rnd.choice((5, 7, 9)))
            d.polygon([(mx, my - r), (mx + r, my), (mx, my + r), (mx - r, my)],
                      outline=color, width=w)
    return layer


# ---------- темы ----------

@dataclass
class Theme:
    key: str
    title: str
    wallpaper: tuple                  # четыре угловых цвета для mesh_gradient
    pattern: Optional[str] = None     # "snow" или ничего
    pattern_color: tuple = (255, 255, 255, 18)
    bubble_in: tuple = (39, 39, 42, 235)
    bubble_out: tuple = (61, 125, 230, 255)
    bubble_out_grad: Optional[tuple] = None   # 4 угла градиента на весь чат
    text_in: tuple = (255, 255, 255, 255)
    text_out: tuple = (255, 255, 255, 255)
    time_in: tuple = (255, 255, 255, 110)
    time_out: tuple = (255, 255, 255, 170)
    tick: tuple = (255, 255, 255, 220)
    link: tuple = (110, 190, 255, 255)
    header_bg: tuple = (26, 26, 28, 225)
    header_text: tuple = (255, 255, 255, 255)
    header_sub: tuple = (160, 160, 168, 255)
    header_accent: tuple = (61, 125, 230, 255)
    glass_header: bool = False        # айосовские «стеклянные» пилюли в шапке
    reply_line: tuple = (240, 160, 60, 255)
    reply_name: tuple = (240, 160, 60, 255)
    input_bg: tuple = (40, 40, 44, 235)
    input_text: tuple = (140, 140, 148, 255)
    bar_bg: tuple = (20, 20, 22, 245)
    status_text: tuple = (255, 255, 255, 255)
    font_pt: float = 17.0             # кегль текста сообщения
    max_bubble: float = 0.78          # доля ширины экрана
    dark: bool = True


THEMES = {
    "ios_dark": Theme(
        key="ios_dark",
        title="тёмная (дефолт)",
        wallpaper=((28, 38, 52), (22, 30, 43), (18, 25, 36), (30, 41, 56)),
        bubble_in=(39, 39, 42, 240),
        bubble_out=(60, 120, 228, 255),
        bubble_out_grad=((74, 138, 244), (86, 150, 250), (46, 100, 214), (58, 112, 226)),
        font_pt=17.6,
        max_bubble=0.795,
    ),
    "ios_teal": Theme(
        key="ios_teal",
        title="бирюзовая (как у тебя)",
        # обои у референса — чистый чёрный с узором, а не тёмно-синий градиент
        wallpaper=((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)),
        pattern="snow",
        pattern_color=(26, 54, 92, 150),
        bubble_in=(32, 30, 35, 250),
        bubble_out=(66, 148, 172, 255),
        # диагональ: бирюза справа-сверху, синий слева-снизу
        bubble_out_grad=((69, 154, 166), (78, 172, 187), (53, 122, 192), (78, 172, 190)),
        time_out=(255, 255, 255, 190),
        header_bg=(28, 28, 30, 205),
        glass_header=True,
        font_pt=17.6,
        max_bubble=0.795,
    ),
}


# ---------- модель сообщения ----------

@dataclass
class Msg:
    text: str = ""
    out: bool = False                 # True — наше, справа
    time: str = "12:00"
    reaction: Optional[str] = None    # эмодзи реакции
    reaction_count: int = 1
    reactor: Optional[str] = None            # чья аватарка стоит рядом с реакцией
    reactor_photo: Optional[Image.Image] = None
    reply_name: Optional[str] = None
    reply_text: Optional[str] = None
    photo: Optional[Image.Image] = None
    date: Optional[str] = None        # разделитель даты ПЕРЕД этим сообщением
    read: bool = True                 # две галочки vs одна
    forwarded_from: Optional[str] = None     # «Переслано от ...»
    forwarded_photo: Optional[Image.Image] = None
    views: Optional[int] = None       # счётчик просмотров рядом со временем
    kind: str = "text"                # text | voice | videonote | file | call | sticker
    duration: str = ""                # «0:57» для голосового и кружка
    file_name: str = ""
    file_size: str = ""
    call_missed: bool = False
    sticker: str = ""                 # эмодзи, если kind == "sticker"


# Метрики в пойнтах. Взяты из макета iOS-телеграма.
# Всё ниже снято пипеткой с реального скриншота, а не подобрано на глаз:
# однострочный пузырь ровно 33pt, двустрочный 56pt -> строка 23pt, паддинг 5pt.
# Ширины семи пузырей сходятся на кегле 17.6pt с разбросом меньше 1pt.
PAD_X, PAD_Y = 10, 5
BUBBLE_R = 14
MAX_BUBBLE = 0.795 * SCREEN_W_PT
EDGE = 10
GAP_SAME, GAP_DIFF = 2.5, 7
TIME_PT = 11.2
TIME_GAP = 8.2
TICK_W = 11
PHOTO_MAX = 250
# голосовое: пузырь 70pt, кружок play 44pt, волна из палок 2.3pt с зазором 2.1pt
VOICE_H = 70
VOICE_PLAY_D = 44
WAVE_X = 54
WAVE_BAR, WAVE_GAP, WAVE_MAX_H = 2.3, 2.1, 22
VNOTE_D = 230
STICKER_D = 140
FILE_H, CALL_H = 70, 58
REPLY_H = 40
REACT_H = 30
FWD_H = 41


def _tick(d: ImageDraw.ImageDraw, x: int, y: int, color, double: bool) -> None:
    """Галочки прочтения: одна или две внахлёст."""
    w = max(1, PX(1.4))
    s = PX(4.5)
    def one(ox):
        d.line((ox, y + s * 0.55, ox + s * 0.62, y + s * 1.15), fill=color, width=w)
        d.line((ox + s * 0.62, y + s * 1.15, ox + s * 1.85, y - s * 0.28), fill=color, width=w)
    one(x)
    if double:
        one(x + PX(4.2))


def _dur_seconds(text: str) -> int:
    try:
        m, sec = text.split(":")
        return int(m) * 60 + int(sec)
    except Exception:
        return 30


def _wave_heights(seed: str, n: int) -> list:
    """Форма волны у голосового — псевдослучайная, но стабильная для одного
    сообщения: иначе при перерисовке картинка «дёргалась» бы."""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        # две синусоиды разной частоты дают «речевой» рисунок с паузами,
        # чистый рандом выглядит как ровная щётка
        env = 0.45 + 0.55 * abs(math.sin(i / 7.3)) * abs(math.cos(i / 17.1 + 0.7))
        out.append(max(0.07, min(1.0, env * rnd.uniform(0.25, 1.25))))
    return out


def _measure(msg: Msg, th: Theme) -> dict:
    f = font(th.font_pt)
    ft = font(TIME_PT, 500)
    emoji_px = PX(th.font_pt * 1.15)
    max_text_w = PX(SCREEN_W_PT * th.max_bubble) - PX(PAD_X) * 2
    line_h = PX(th.font_pt * 1.307)

    max_bubble_w = PX(SCREEN_W_PT * th.max_bubble)

    if msg.kind == "videonote":
        extra = PX(REACT_H + 6) if msg.reaction else 0
        return dict(kind=msg.kind, lines=[], line_h=0, w=PX(VNOTE_D), h=PX(VNOTE_D) + extra,
                    time_inline=False, time_w=0, photo_h=0, photo_w=0, inset=0,
                    emoji_px=PX(th.font_pt * 1.15))
    if msg.kind == "sticker":
        return dict(kind=msg.kind, lines=[], line_h=0, w=PX(STICKER_D),
                    h=PX(STICKER_D) + PX(24),
                    time_inline=False, time_w=0, photo_h=0, photo_w=0, inset=0,
                    emoji_px=PX(th.font_pt * 1.15))
    if msg.kind == "voice":
        w = PX(290 + min(1.0, _dur_seconds(msg.duration) / 60) * 38)
        return dict(kind=msg.kind, lines=[], line_h=0, w=int(w), h=PX(VOICE_H),
                    time_inline=False, time_w=0, photo_h=0, photo_w=0, inset=0,
                    emoji_px=PX(th.font_pt * 1.15))
    if msg.kind in ("file", "call"):
        fw = font(16, 600).getlength(msg.file_name or ("Пропущенный звонок" if msg.call_missed
                                                       else "Входящий звонок"))
        w = int(min(max_bubble_w, PX(VOICE_PLAY_D + 34) + fw + PX(24)))
        return dict(kind=msg.kind, lines=[], line_h=0, w=w,
                    h=PX(FILE_H if msg.kind == "file" else CALL_H),
                    time_inline=False, time_w=0, photo_h=0, photo_w=0, inset=0,
                    emoji_px=PX(th.font_pt * 1.15))

    lines = wrap_text(msg.text, f, max_text_w, emoji_px) if msg.text.strip() else []
    views_w = (PX(19) + int(ft.getlength(str(msg.views)))) if msg.views is not None else 0
    time_w = int(ft.getlength(msg.time)) + (PX(TICK_W) if msg.out else 0) + views_w

    def fits(ls):
        return bool(ls) and measure(ls[-1], f, emoji_px) + PX(TIME_GAP) + time_w <= max_text_w

    time_inline = fits(lines)
    if lines and not time_inline:
        # пробуем перелить текст так, чтобы время влезло в конец, не добавив строк
        narrow = wrap_text(msg.text, f, max_text_w - time_w - PX(TIME_GAP), emoji_px)
        if len(narrow) == len(lines) and fits(narrow):
            lines, time_inline = narrow, True
    text_w = max((measure(l, f, emoji_px) for l in lines), default=0)

    react_w = 0
    if msg.reaction:
        react_w = PX(16) + PX(REACT_H * 0.62) + PX(6) + int(font(13.5, 600).getlength(str(msg.reaction_count)))

    fwd_w = 0
    if msg.forwarded_from:
        fwd_w = max(int(font(14, 500).getlength("Переслано от")),
                    PX(21) + int(font(15, 600).getlength(msg.forwarded_from))) + PX(4)

    reply_w = 0
    if msg.reply_name:
        rf = font(14, 500)
        reply_w = min(max_text_w, max(int(rf.getlength(msg.reply_name or "")),
                                      int(rf.getlength(msg.reply_text or ""))) + PX(14))

    content_w = max(text_w + (PX(TIME_GAP) + time_w if time_inline else 0),
                    reply_w, react_w, fwd_w, 0)
    content_w = min(content_w, max_bubble_w - PX(PAD_X) * 2)
    photo_w = photo_h = inset = 0
    if msg.photo is not None:
        # пузырь тянется под подпись, но не уже картинки по умолчанию
        bubble_w = max(min(max_bubble_w, content_w + PX(PAD_X) * 2), PX(PHOTO_MAX))
        inset = PX(3) if (msg.forwarded_from or msg.reply_name) else 0
        photo_w = int(bubble_w) - inset * 2
        pw, ph = msg.photo.size
        photo_h = min(int(photo_w * ph / pw), PX(360))
    else:
        bubble_w = content_w + PX(PAD_X) * 2
    bubble_w = max(bubble_w, PX(56))

    h = 0
    if photo_h:
        h += photo_h + inset
    if msg.forwarded_from:
        h += PX(FWD_H)
    if msg.reply_name:
        h += PX(REPLY_H) + PX(4)
    if lines:
        h += line_h * len(lines)
    if lines and not time_inline:
        h += PX(TIME_PT * 1.2)
    if msg.reaction:
        h += PX(REACT_H) + PX(5)
    if photo_h and not lines and not msg.reaction:
        bubble_h = h
    else:
        bubble_h = h + PX(PAD_Y) * 2

    return dict(kind=msg.kind, lines=lines, line_h=line_h, w=int(bubble_w), h=int(bubble_h),
                time_inline=time_inline, time_w=time_w, photo_h=photo_h, inset=inset,
                photo_w=photo_w, emoji_px=emoji_px)


def _play_circle(layer: Image.Image, d: ImageDraw.ImageDraw, cx: int, cy: int,
                 diam: int, bg, glyph_color, kind: str) -> None:
    r = diam // 2
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=bg)
    if kind == "play":
        t = diam * 0.28
        d.polygon([(cx - t * 0.6, cy - t), (cx - t * 0.6, cy + t), (cx + t, cy)], fill=glyph_color)
    elif kind == "doc":
        w_, h_ = diam * 0.26, diam * 0.34
        d.rounded_rectangle((cx - w_, cy - h_, cx + w_, cy + h_), radius=PX(2), fill=glyph_color)
        d.polygon([(cx + w_ - PX(7), cy - h_), (cx + w_, cy - h_ + PX(7)),
                   (cx + w_ - PX(7), cy - h_ + PX(7))], fill=bg)
    else:  # телефонная трубка
        ic = glyph("📞", int(diam * 0.52), glyph_color)
        if ic is not None:
            layer.alpha_composite(ic, (cx - ic.width // 2, cy - ic.height // 2))


def _transcribe_btn(d: ImageDraw.ImageDraw, x: int, y: int, accent) -> None:
    """Кнопка «расшифровать в текст» — стрелка и буква A в скруглённом поле."""
    d.rounded_rectangle((x, y, x + PX(40), y + PX(34)), radius=PX(9),
                        fill=(255, 255, 255, 30))
    d.line((x + PX(9), y + PX(17), x + PX(18), y + PX(17)), fill=accent, width=PX(2))
    d.polygon([(x + PX(18), y + PX(13)), (x + PX(22), y + PX(17)), (x + PX(18), y + PX(21))],
              fill=accent)
    d.text((x + PX(23), y + PX(8)), "A", font=font(14, 600), fill=accent)


def _draw_media_row(layer, d, msg: Msg, th: Theme, m: dict, text_color, time_color, ft) -> None:
    w, h = m["w"], m["h"]
    accent = (255, 255, 255, 255) if msg.out else th.header_accent
    circle_bg = (255, 255, 255, 235) if msg.out else (105, 180, 234, 255)
    glyph_color = th.bubble_out[:3] + (255,) if msg.out else (255, 255, 255, 255)
    cx, cy = PX(11) + PX(VOICE_PLAY_D) // 2, h // 2 if m["kind"] != "voice" else PX(33)

    if m["kind"] == "voice":
        _play_circle(layer, d, cx, cy, PX(VOICE_PLAY_D), circle_bg, glyph_color, "play")
        x0 = PX(WAVE_X)
        x1 = w - PX(52)
        pitch = PX(WAVE_BAR + WAVE_GAP)
        n = max(4, int((x1 - x0) / pitch))
        wave_col = (255, 255, 255, 190) if msg.out else (94, 138, 170, 255)
        mid = cy
        for i, k in enumerate(_wave_heights(msg.duration + msg.time, n)):
            bh = max(PX(1.5), int(PX(WAVE_MAX_H) * k / 2))
            bx = x0 + i * pitch
            d.rounded_rectangle((bx, mid - bh, bx + PX(WAVE_BAR), mid + bh),
                                radius=PX(WAVE_BAR / 2), fill=wave_col)
        d.text((x0, cy + PX(14)), msg.duration or "0:30", font=font(13, 500), fill=time_color)
        _transcribe_btn(d, w - PX(48), PX(14), accent)
    elif m["kind"] == "file":
        _play_circle(layer, d, cx, cy, PX(VOICE_PLAY_D), circle_bg, glyph_color, "doc")
        d.text((PX(WAVE_X + 11), PX(15)), msg.file_name or "document.pdf",
               font=font(16, 600), fill=text_color)
        d.text((PX(WAVE_X + 11), PX(38)), msg.file_size or "2,4 МБ",
               font=font(14), fill=time_color)
    else:  # звонок
        _play_circle(layer, d, cx, cy, PX(VOICE_PLAY_D), circle_bg, glyph_color, "call")
        title = "Пропущенный звонок" if msg.call_missed else "Входящий звонок"
        d.text((PX(WAVE_X + 11), PX(12)), title, font=font(16, 600), fill=text_color)
        d.text((PX(WAVE_X + 11), PX(34)), msg.duration or "не отвечено",
               font=font(14), fill=time_color)

    tw = int(ft.getlength(msg.time))
    tx = w - PX(PAD_X) - tw - (PX(TICK_W) if msg.out else 0)
    ty = h - PX(22)
    d.text((tx, ty), msg.time, font=ft, fill=time_color)
    if msg.out:
        _tick(d, tx + tw + PX(4), ty + PX(4), th.tick, msg.read)


def _draw_bare(canvas: Image.Image, msg: Msg, th: Theme, m: dict, x: int, y: int) -> None:
    """Кружок и стикер живут без пузыря — прямо на обоях."""
    D = m["w"]
    layer = Image.new("RGBA", (D + PX(150), D + PX(40)), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    ft = font(TIME_PT, 500)

    if m["kind"] == "videonote":
        src = msg.photo or mesh_gradient((D, D), ((90, 96, 110), (70, 76, 90),
                                                 (60, 64, 76), (84, 90, 104)))
        pic = src.convert("RGB")
        side = min(pic.size)
        pic = pic.crop(((pic.width - side) // 2, (pic.height - side) // 2,
                        (pic.width + side) // 2, (pic.height + side) // 2)).resize((D, D), Image.LANCZOS)
        mask = Image.new("L", (D * 2, D * 2), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, D * 2 - 1, D * 2 - 1), fill=255)
        layer.paste(pic.convert("RGBA"), (0, 0), mask.resize((D, D), Image.LANCZOS))
        # длительность в левом нижнем углу описанного квадрата
        d.text((int(D * 0.14), D - PX(24)), msg.duration or "0:10",
               font=font(14, 500), fill=(255, 255, 255, 240))
        # перечёркнутый динамик по центру низа
        mx, my = D // 2, D - PX(42)
        d.polygon([(mx - PX(9), my - PX(4)), (mx - PX(4), my - PX(4)), (mx + PX(1), my - PX(9)),
                   (mx + PX(1), my + PX(9)), (mx - PX(4), my + PX(4)), (mx - PX(9), my + PX(4))],
                  fill=(255, 255, 255, 235))
        d.line((mx + PX(5), my - PX(5), mx + PX(13), my + PX(5)), fill=(255, 255, 255, 235), width=PX(2))
        d.line((mx + PX(13), my - PX(5), mx + PX(5), my + PX(5)), fill=(255, 255, 255, 235), width=PX(2))
        _transcribe_btn(d, D + PX(14), D - PX(78), th.header_accent)
        d.text((D + PX(66), D - PX(68)), msg.time, font=ft, fill=(255, 255, 255, 200))
    else:  # стикер
        em = render_emoji(msg.sticker or "🙂", D)
        if em is not None:
            layer.alpha_composite(em, (0, 0))
        tw = int(ft.getlength(msg.time))
        # время у стикера стоит ПОД ним, иначе налезает на картинку
        d.rounded_rectangle((D - tw - PX(18), D + PX(2), D, D + PX(24)),
                            radius=PX(11), fill=(0, 0, 0, 105))
        d.text((D - tw - PX(9), D + PX(5)), msg.time, font=ft, fill=(255, 255, 255, 235))

    canvas.alpha_composite(layer, (x, y))

    if msg.reaction:                       # реакция уходит ПОД кружок, отдельным чипом
        rl = Image.new("RGBA", (PX(90), PX(REACT_H)), (0, 0, 0, 0))
        rd = ImageDraw.Draw(rl)
        ew = PX(REACT_H * 0.66)
        rd.rounded_rectangle((0, 0, PX(9) + ew * 2 + PX(14), PX(REACT_H)),
                             radius=PX(REACT_H / 2), fill=th.header_accent[:3] + (80,))
        em = render_emoji(msg.reaction, int(ew))
        if em is not None:
            rl.alpha_composite(em, (PX(9), int((PX(REACT_H) - ew) / 2)))
        rl.alpha_composite(_avatar(int(ew), msg.reactor, msg.reactor_photo)
                           if (msg.reactor or msg.reactor_photo) else _blank_avatar(int(ew)),
                           (PX(9) + int(ew) + PX(5), int((PX(REACT_H) - ew) / 2)))
        canvas.alpha_composite(rl, (x, y + D + PX(6)))


def _draw_bubble(canvas: Image.Image, msg: Msg, th: Theme, m: dict,
                 x: int, y: int, tail: bool, grad: Optional[Image.Image]) -> None:
    w, h = m["w"], m["h"]
    mask, dx = bubble_mask(w, h, PX(BUBBLE_R),
                           ("right" if msg.out else "left") if tail else None)
    if msg.out and grad is not None:
        fill = grad.crop((0, y, mask.width, y + h)).convert("RGBA")
        alpha = mask
    else:
        color = th.bubble_out if msg.out else th.bubble_in
        fill = Image.new("RGBA", mask.size, color)
        alpha = ImageChops.multiply(mask, Image.new("L", mask.size, color[3]))
    fill.putalpha(alpha)
    canvas.alpha_composite(fill, (x - dx, y))

    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = font(th.font_pt)
    ft = font(TIME_PT, 500)
    text_color = th.text_out if msg.out else th.text_in
    time_color = th.time_out if msg.out else th.time_in
    bare_photo = m["photo_h"] and not m["lines"] and not msg.forwarded_from and not msg.reply_name
    if m["kind"] in ("voice", "file", "call"):
        _draw_media_row(layer, d, msg, th, m, text_color, time_color, ft)
        canvas.alpha_composite(layer, (x, y))
        return

    cy = 0 if bare_photo else PX(PAD_Y)

    if msg.forwarded_from:
        fx, fy = PX(PAD_X), cy
        accent = th.text_out if msg.out else th.header_accent
        d.text((fx, fy), "Переслано от", font=font(14, 500), fill=accent)
        av = _avatar(PX(17), msg.forwarded_from, msg.forwarded_photo)
        layer.alpha_composite(av, (fx, fy + PX(20)))
        d.text((fx + PX(21), fy + PX(19)), msg.forwarded_from,
               font=font(15, 600), fill=accent)
        cy += PX(FWD_H)

    if msg.reply_name:
        rx, ry = PX(PAD_X), cy
        rw = w - PX(PAD_X) * 2
        d.rounded_rectangle((rx, ry, rx + rw, ry + PX(REPLY_H)), radius=PX(4),
                            fill=(255, 255, 255, 26))
        d.rounded_rectangle((rx, ry, rx + PX(3), ry + PX(REPLY_H)), radius=PX(1.5),
                            fill=th.reply_line)
        d.text((rx + PX(9), ry + PX(4)), msg.reply_name, font=font(14, 600), fill=th.reply_name)
        d.text((rx + PX(9), ry + PX(21)), (msg.reply_text or "")[:40], font=font(14), fill=text_color)
        cy += PX(REPLY_H) + PX(4)

    if msg.photo is not None:
        ins = m["inset"]
        pw_, ph_ = m["photo_w"], m["photo_h"]
        pic = msg.photo.convert("RGB").resize((pw_, ph_), Image.LANCZOS)
        # у вставленной внутрь пузыря картинки скругления мельче, чем у него самого
        rad = PX(6) if ins else PX(BUBBLE_R)
        pm, _ = bubble_mask(pw_, ph_, rad, None)
        if m["lines"] and not ins:
            ImageDraw.Draw(pm).rectangle((0, ph_ - rad, pw_, ph_), fill=255)
        layer.paste(pic.convert("RGBA"), (ins, cy), pm)
        cy += ph_ + (PX(PAD_Y) if m["lines"] else 0)

    for i, line in enumerate(m["lines"]):
        draw_runs(layer, (PX(PAD_X), cy), line, f, text_color, m["emoji_px"], m["line_h"])
        cy += m["line_h"]

    if msg.reaction:
        ew = PX(REACT_H * 0.66)
        solo = msg.reaction_count <= 1
        tail_w = ew if solo else int(font(14, 600).getlength(str(msg.reaction_count)))
        cw = PX(9) + ew + PX(5) + tail_w + PX(9)
        d.rounded_rectangle((PX(PAD_X), cy, PX(PAD_X) + cw, cy + PX(REACT_H)),
                            radius=PX(REACT_H / 2),
                            fill=(255, 255, 255, 45) if msg.out else th.header_accent[:3] + (80,))
        em = render_emoji(msg.reaction, int(ew))
        if em is not None:
            layer.alpha_composite(em, (PX(PAD_X) + PX(9), int(cy + (PX(REACT_H) - ew) / 2)))
        if solo:
            av = (_avatar(int(ew), msg.reactor, msg.reactor_photo)
                  if (msg.reactor or msg.reactor_photo) else _blank_avatar(int(ew)))
            layer.alpha_composite(av, (PX(PAD_X) + PX(9) + ew + PX(5),
                                       int(cy + (PX(REACT_H) - ew) / 2)))
        else:
            d.text((PX(PAD_X) + PX(9) + ew + PX(5), cy + PX(5)), str(msg.reaction_count),
                   font=font(14, 600), fill=text_color)
        cy += PX(REACT_H) + PX(5)

    # время: либо в хвосте последней строки, либо отдельной строкой справа
    tw = int(ft.getlength(msg.time))
    views_w = (PX(19) + int(ft.getlength(str(msg.views)))) if msg.views is not None else 0
    tx = w - PX(PAD_X) - tw - (PX(TICK_W) if msg.out else 0)
    if m["photo_h"] and not m["lines"]:
        ty = m["photo_h"] - PX(22)
        d.rounded_rectangle((tx - PX(7), ty - PX(3), w - PX(5), ty + PX(16)),
                            radius=PX(10), fill=(0, 0, 0, 90))
        time_color = (255, 255, 255, 235)
    elif m["time_inline"]:
        ty = cy - m["line_h"] + PX(th.font_pt * 0.28)
    else:
        ty = cy
        cy += PX(TIME_PT * 1.2)
    if msg.views is not None:
        ex, ey = tx - views_w, ty + PX(7)
        d.ellipse((ex, ey - PX(4), ex + PX(14), ey + PX(4)), outline=time_color, width=PX(1.3))
        d.ellipse((ex + PX(5), ey - PX(2), ex + PX(9), ey + PX(2)), fill=time_color)
        d.text((ex + PX(17), ty), str(msg.views), font=ft, fill=time_color)
    d.text((tx, ty), msg.time, font=ft, fill=time_color)
    if msg.out:
        _tick(d, tx + tw + PX(4), ty + PX(4), th.tick, msg.read)

    canvas.alpha_composite(layer, (x, y))


# ---------- обвязка экрана ----------

STATUS_H, HEADER_H, INPUT_H, HOME_H = 59, 44, 52, 34
PINNED_H = 52


def _glass(canvas: Image.Image, box, radius: int, tint) -> None:
    """Полупрозрачная «стеклянная» пилюля: вырезали фон, размыли, затонировали."""
    x0, y0, x1, y1 = box
    region = canvas.crop(box).filter(ImageFilter.GaussianBlur(PX(12)))
    overlay = Image.new("RGBA", region.size, tint)
    region = Image.alpha_composite(region, overlay)
    mask = Image.new("L", (region.width * 2, region.height * 2), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, mask.width - 1, mask.height - 1),
                                           radius=radius * 2, fill=255)
    canvas.paste(region, (x0, y0), mask.resize(region.size, Image.LANCZOS))


def _avatar(size: int, name: str, img: Optional[Image.Image]) -> Image.Image:
    mask = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 2 - 1, size * 2 - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    if img is not None:
        src = img.convert("RGB")
        side = min(src.size)
        src = src.crop(((src.width - side) // 2, (src.height - side) // 2,
                        (src.width + side) // 2, (src.height + side) // 2))
        base = src.resize((size, size), Image.LANCZOS).convert("RGBA")
    else:
        palette = [((255, 81, 106), (255, 136, 94)), ((84, 203, 104), (168, 233, 106)),
                   ((102, 136, 255), (132, 194, 255)), ((232, 168, 56), (255, 209, 106)),
                   ((167, 104, 255), (216, 140, 255))]
        top, bot = palette[sum(map(ord, name or "?")) % len(palette)]
        base = mesh_gradient((size, size), (top, top, bot, bot)).convert("RGBA")
        letters = "".join(w[0] for w in (name or "?").split()[:2]).upper()
        d = ImageDraw.Draw(base)
        f = font(size / SCALE * 0.42, 600)
        tw = f.getlength(letters)
        d.text(((size - tw) / 2, size * 0.27), letters, font=f, fill=(255, 255, 255, 255))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    return out


def _blank_avatar(size: int) -> Image.Image:
    """Безликая аватарка: силуэт на сером — так выглядит собеседник без фото."""
    img = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    S = size * 2
    d.ellipse((0, 0, S - 1, S - 1), fill=(126, 130, 140, 255))
    d.ellipse((S * 0.32, S * 0.2, S * 0.68, S * 0.56), fill=(215, 218, 224, 255))
    d.ellipse((S * 0.18, S * 0.62, S * 0.82, S * 1.26), fill=(215, 218, 224, 255))
    return img.resize((size, size), Image.LANCZOS)


def _status_bar(canvas: Image.Image, th: Theme, clock: str) -> None:
    d = ImageDraw.Draw(canvas)
    W = canvas.width
    d.rounded_rectangle(((W - PX(126)) / 2, PX(11), (W + PX(126)) / 2, PX(48)),
                        radius=PX(18.5), fill=(0, 0, 0, 255))
    f = font(16.5, 600)
    tw = f.getlength(clock)
    d.text((PX(56) - tw / 2, PX(19)), clock, font=f, fill=th.status_text)
    cy = PX(31)
    x = W - PX(78)
    for i in range(4):                                     # палки сети
        bh = PX(3.5 + i * 2.2)
        d.rounded_rectangle((x + i * PX(5), cy + PX(5) - bh, x + i * PX(5) + PX(3.2), cy + PX(5)),
                            radius=PX(1), fill=th.status_text)
    x = W - PX(54)
    for i, r in enumerate((PX(3), PX(7), PX(11))):         # вайфай
        d.arc((x - r + PX(8), cy + PX(5) - r, x + r + PX(8), cy + PX(5) + r),
              start=215, end=325, fill=th.status_text, width=PX(1.8))
    d.ellipse((x + PX(6.5), cy + PX(3), x + PX(9.5), cy + PX(6)), fill=th.status_text)
    bx = W - PX(30)                                        # батарейка
    bat, bd = overlay(canvas)
    bd.rounded_rectangle((bx, cy - PX(5.5), bx + PX(23), cy + PX(5.5)), radius=PX(3.2),
                         outline=th.status_text[:3] + (110,), width=PX(1.2))
    bd.rounded_rectangle((bx + PX(24.4), cy - PX(2), bx + PX(25.8), cy + PX(2)),
                         radius=PX(1), fill=th.status_text[:3] + (110,))
    canvas.alpha_composite(bat)
    d.rounded_rectangle((bx + PX(2), cy - PX(3.5), bx + PX(2) + PX(12), cy + PX(3.5)),
                        radius=PX(1.8), fill=th.status_text)



def _ellipsize(text: str, f: ImageFont.FreeTypeFont, max_w: int) -> str:
    """Длинное имя в шапке телеграм режет многоточием, а не переносит."""
    if measure(text, f, PX(17)) <= max_w:
        return text
    cut = text
    while cut and measure(cut + "…", f, max_w) > max_w:
        cut = cut[:-1]
    return cut.rstrip() + "…"


def _pinned(canvas: Image.Image, th: Theme, text: str) -> None:
    W = canvas.width
    top = PX(STATUS_H + HEADER_H) + PX(6)
    x0, x1 = PX(14), W - PX(14)
    if th.glass_header:
        _glass(canvas, (x0, top, x1, top + PX(PINNED_H)), PX(20), th.header_bg)
    else:
        strip = canvas.crop((0, top, W, top + PX(PINNED_H))).filter(ImageFilter.GaussianBlur(PX(14)))
        strip.alpha_composite(Image.new("RGBA", strip.size, th.header_bg))
        canvas.paste(strip, (0, top))
        x0 = PX(16)
    layer, d = overlay(canvas)
    d.rounded_rectangle((x0 + PX(12), top + PX(10), x0 + PX(14), top + PX(PINNED_H) - PX(10)),
                        radius=PX(1), fill=th.header_accent)
    d.text((x0 + PX(22), top + PX(9)), "Закреплённое сообщение",
           font=font(14, 600), fill=th.header_accent)
    d.text((x0 + PX(22), top + PX(27)), _ellipsize(text, font(15), W - x0 - PX(90)),
           font=font(15), fill=th.header_text)
    canvas.alpha_composite(layer)
    # канцелярская кнопка справа: рисуем прямо и наклоняем поворотом
    pin = Image.new("RGBA", (PX(30), PX(30)), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pin)
    c = PX(15)
    pd.rounded_rectangle((c - PX(5), PX(4), c + PX(5), PX(14)), radius=PX(2), fill=th.header_sub)
    pd.rounded_rectangle((c - PX(8), PX(13), c + PX(8), PX(16)), radius=PX(1.5), fill=th.header_sub)
    pd.line((c, PX(16), c, PX(25)), fill=th.header_sub, width=PX(2))
    canvas.alpha_composite(pin.rotate(-35, resample=Image.BICUBIC),
                           (x1 - PX(44), int(top + PX(PINNED_H / 2) - PX(15))))


def _header(canvas: Image.Image, th: Theme, name: str, subtitle: str,
            avatar: Optional[Image.Image], unread: Optional[int]) -> None:
    W = canvas.width
    top, bottom = 0, PX(STATUS_H + HEADER_H)
    if th.glass_header:
        back_w = PX(50 + (24 if unread else 0))
        _glass(canvas, (PX(14), PX(65), PX(14) + back_w, PX(65) + PX(34)), PX(17), th.header_bg)
        _glass(canvas, (PX(100), PX(61), W - PX(72), PX(61) + PX(42)), PX(21), th.header_bg)
    else:
        strip = Image.new("RGBA", (W, bottom), (0, 0, 0, 0))
        blurred = canvas.crop((0, 0, W, bottom)).filter(ImageFilter.GaussianBlur(PX(14)))
        strip.alpha_composite(blurred)
        strip.alpha_composite(Image.new("RGBA", (W, bottom), th.header_bg))
        canvas.paste(strip, (0, 0))
        hair, hd = overlay(canvas)
        hd.line((0, bottom, W, bottom), fill=(255, 255, 255, 30), width=max(1, PX(0.5)))
        canvas.alpha_composite(hair)

    d = ImageDraw.Draw(canvas)
    cy = PX(STATUS_H + HEADER_H / 2)
    bx = PX(24)                                            # шеврон «назад»
    d.line((bx + PX(7), cy - PX(7), bx, cy), fill=th.header_accent, width=PX(2.4))
    d.line((bx, cy, bx + PX(7), cy + PX(7)), fill=th.header_accent, width=PX(2.4))
    if unread:
        d.text((bx + PX(13), cy - PX(10)), str(unread), font=font(17, 500), fill=th.header_accent)

    fn, fs = font(17, 600), font(13)
    name = _ellipsize(name, fn, W - PX(220))
    nw = measure(name, fn, PX(17))
    draw_runs(canvas, ((W - nw) / 2, cy - PX(16)), name, fn, th.header_text, PX(17), PX(20))
    sw = fs.getlength(subtitle)
    d.text(((W - sw) / 2, cy + PX(4)), subtitle, font=fs, fill=th.header_sub)

    av = _avatar(PX(37), name, avatar)
    canvas.alpha_composite(av, (W - PX(16) - PX(37), cy - PX(18)))


def _input_bar(canvas: Image.Image, th: Theme) -> None:
    W, H = canvas.size
    top = H - PX(HOME_H + INPUT_H)
    strip = canvas.crop((0, top, W, H)).filter(ImageFilter.GaussianBlur(PX(14)))
    strip.alpha_composite(Image.new("RGBA", strip.size, th.bar_bg))
    canvas.paste(strip, (0, top))
    d = ImageDraw.Draw(canvas)
    cy = top + PX(INPUT_H / 2)
    clip = glyph("📎", PX(30), th.input_text)
    if clip is not None:
        canvas.alpha_composite(clip, (PX(14), int(cy - PX(15))))
    fx0, fx1 = PX(50), W - PX(46)
    fld, fd = overlay(canvas)
    fd.rounded_rectangle((fx0, cy - PX(18), fx1, cy + PX(18)), radius=PX(18), fill=th.input_bg)
    canvas.alpha_composite(fld)
    d.text((fx0 + PX(15), cy - PX(11)), "Сообщение", font=font(16.5), fill=th.input_text)
    # иконка стикеров у правого края поля
    sx = fx1 - PX(26)
    d.ellipse((sx - PX(11), cy - PX(11), sx + PX(11), cy + PX(11)),
              outline=th.input_text, width=PX(2))
    d.line((sx, cy - PX(6), sx, cy), fill=th.input_text, width=PX(2))
    d.line((sx, cy, sx + PX(5), cy + PX(3)), fill=th.input_text, width=PX(2))
    # микрофон
    mx = W - PX(27)
    d.rounded_rectangle((mx - PX(4), cy - PX(11), mx + PX(4), cy + PX(2)), radius=PX(4),
                        fill=th.input_text)
    d.arc((mx - PX(8), cy - PX(6), mx + PX(8), cy + PX(8)), start=0, end=180,
          fill=th.input_text, width=PX(2))
    d.line((mx, cy + PX(8), mx, cy + PX(12)), fill=th.input_text, width=PX(2))
    d.rounded_rectangle(((W - PX(108)) / 2, H - PX(20), (W + PX(108)) / 2, H - PX(15)),
                        radius=PX(3), fill=(255, 255, 255, 190) if th.dark else (0, 0, 0, 170))


def _scroll_button(canvas: Image.Image, th: Theme) -> None:
    W, H = canvas.size
    cx = W - PX(37)
    cy = H - PX(HOME_H + INPUT_H) - PX(38)
    r = PX(23)
    region = canvas.crop((cx - r, cy - r, cx + r, cy + r)).filter(ImageFilter.GaussianBlur(PX(10)))
    region.alpha_composite(Image.new("RGBA", region.size, (44, 44, 48, 205)))
    mask = Image.new("L", (r * 4, r * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, r * 4 - 1, r * 4 - 1), fill=255)
    canvas.paste(region, (cx - r, cy - r), mask.resize((r * 2, r * 2), Image.LANCZOS))
    d = ImageDraw.Draw(canvas)
    d.line((cx - PX(9), cy - PX(4), cx, cy + PX(5)), fill=(235, 235, 240, 255), width=PX(2.4))
    d.line((cx, cy + PX(5), cx + PX(9), cy - PX(4)), fill=(235, 235, 240, 255), width=PX(2.4))


def _date_pill(canvas: Image.Image, th: Theme, text: str, y: int) -> int:
    layer, d = overlay(canvas)
    f = font(13, 600)
    tw = int(f.getlength(text))
    w, h = tw + PX(22), PX(23)
    x = (canvas.width - w) // 2
    d.rounded_rectangle((x, y, x + w, y + h), radius=PX(11.5), fill=(0, 0, 0, 105))
    d.text((x + PX(11), y + PX(3.5)), text, font=f, fill=(255, 255, 255, 235))
    canvas.alpha_composite(layer)
    return h + PX(10)


# ---------- публичная функция ----------

def make_chat_screenshot(messages: Sequence[Msg], *, theme: str = "ios_dark",
                         contact_name: str = "Контакт",
                         subtitle: str = "был(а) 2 минуты назад",
                         avatar: Optional[Image.Image] = None,
                         unread: Optional[int] = None,
                         pinned: Optional[str] = None,
                         clock: str = "13:17",
                         up_to: Optional[int] = None) -> BytesIO:
    """Собирает скриншот чата. up_to ограничивает число показанных сообщений —
    через него потом делается видео: кадр на каждое новое сообщение."""
    th = THEMES.get(theme, THEMES["ios_dark"])
    msgs = list(messages)[:up_to] if up_to is not None else list(messages)

    W, H = PX(SCREEN_W_PT), PX(SCREEN_H_PT)
    canvas = mesh_gradient((W, H), th.wallpaper).convert("RGBA")
    if th.pattern == "snow":
        canvas.alpha_composite(snow_pattern((W, H), th.pattern_color))

    grad = None
    if th.bubble_out_grad:
        grad = mesh_gradient((W, H), th.bubble_out_grad)

    area_top = PX(STATUS_H + HEADER_H) + PX(8) + (PX(PINNED_H + 6) if pinned else 0)
    area_bottom = H - PX(HOME_H + INPUT_H) - PX(8)

    metrics = [_measure(m, th) for m in msgs]
    total = 0
    for i, (m, mm) in enumerate(zip(msgs, metrics)):
        if m.date:
            total += PX(23) + PX(10)
        if i:
            total += PX(GAP_SAME if msgs[i - 1].out == m.out else GAP_DIFF)
        total += mm["h"]

    # если не влезает — верх «уезжает» за шапку, как в настоящем скролле
    y = max(area_top, area_bottom - total) if total < (area_bottom - area_top) else area_bottom - total

    for i, (m, mm) in enumerate(zip(msgs, metrics)):
        if m.date:
            y += _date_pill(canvas, th, m.date, y)
        if i:
            y += PX(GAP_SAME if msgs[i - 1].out == m.out else GAP_DIFF)
        last_of_run = (i == len(msgs) - 1) or (msgs[i + 1].out != m.out)
        x = W - PX(EDGE) - mm["w"] if m.out else PX(EDGE)
        if y + mm["h"] > area_top - PX(40):
            if mm["kind"] in ("videonote", "sticker"):
                _draw_bare(canvas, m, th, mm, x, y)
            else:
                _draw_bubble(canvas, m, th, mm, x, y, last_of_run, grad)
        y += mm["h"]

    _scroll_button(canvas, th)
    _header(canvas, th, contact_name, subtitle, avatar, unread)
    if pinned:
        _pinned(canvas, th, pinned)
    _status_bar(canvas, th, clock)
    _input_bar(canvas, th)

    buf = BytesIO()
    canvas.convert("RGB").save(buf, format="JPEG", quality=94, optimize=True)
    buf.seek(0)
    return buf
