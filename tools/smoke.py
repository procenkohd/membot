"""Быстрая проверка, что всё ещё собирается. Тестов в проекте нет, а рендереров
три, и правка в одном тихо ломает другой — этот скрипт ровно от такого.

Что ловит на практике: пропавшую функцию, несуществующее поле темы, развал
раскладки на каком-то сочетании. Картинки никуда не сохраняются, проверяется
только то, что рендер не падает и отдаёт непустой файл.

    python tools/smoke.py        # молча, если всё хорошо; код возврата 1 при ошибке
"""

import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from PIL import Image

import chatflow
import chatgen
import memegen

KINDS = ("text", "photo", "voice", "videonote", "sticker", "file", "call")
FAILS = []


def check(label: str, fn) -> None:
    try:
        fn()
    except Exception as e:
        FAILS.append(f"{label}: {type(e).__name__}: {e}")


def sample_photo(size=(900, 1100)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (70, 90, 120)).save(buf, "JPEG", quality=85)
    return buf.getvalue()


class _NoBot:
    """Скачивание картинок недоступно — проверяем ветку «фото не пришло»."""
    async def download(self, file_id, destination=None):
        raise RuntimeError("нет сети")


def build_draft(theme: str, mode: str) -> dict:
    d = chatflow.blank_draft()
    d.update(kind=mode, contact_name="Аня", my_name="Никита", theme=theme,
             members=[{"name": "Миша", "avatar": None}] if mode == "group" else [])
    for i, kind in enumerate(KINDS):
        d["speaker"] = -1 if i % 2 else 0
        d["speaker_out"] = bool(i % 2)
        extra = {}
        if kind in ("voice", "videonote"):
            extra = {"duration": "0:25"}
        elif kind == "file":
            extra = {"file_name": "договор.pdf", "file_size": "2,4 МБ"}
        elif kind == "sticker":
            extra = {"sticker": "🗿"}
        chatflow._add(d, kind=kind, text="проверка 🔥" if kind in ("text", "photo") else "",
                      **extra)
    # реакции: один человек, несколько и «больше лиц, чем влезает»
    d["items"][1]["reactions"] = [{"emoji": "😁", "by": [-1]}]
    d["items"][2]["reactions"] = [{"emoji": "🔥", "by": [0, -1]},
                                  {"emoji": "🤡", "by": [0]}]
    d["items"][4]["reactions"] = [{"emoji": "💀", "by": list(range(-1, 7))}]
    d["items"][2]["reply_to"] = 0
    d["items"][3]["date"] = "Сегодня"
    return d


async def run() -> None:
    # 1. все темы, оба режима, все типы сообщений
    for theme in chatgen.THEMES:
        for mode in ("duo", "group"):
            draft = build_draft(theme, mode)
            try:
                pages = await chatflow.render_draft(_NoBot(), draft)
                assert pages and pages[0].getbuffer().nbytes > 1000
            except Exception as e:
                FAILS.append(f"переписка {theme}/{mode}: {type(e).__name__}: {e}")

    # 2. длинная переписка должна разъехаться на несколько экранов
    long_draft = chatflow.blank_draft()
    long_draft.update(contact_name="Аня", my_name="Никита", theme="ios_teal")
    for i in range(40):
        long_draft["speaker_out"] = bool(i % 2)
        chatflow._add(long_draft, kind="text", text=f"реплика {i} подлиннее для объёма")
    pages = await chatflow.render_draft(_NoBot(), long_draft)
    if len(pages) < 2:
        FAILS.append(f"длинная переписка уместилась в {len(pages)} экран, ожидалось больше")

    # 3. счётчик экранов в панели обязан совпадать с тем, что реально рисуется
    counted = chatgen.page_count(chatflow._preview_msgs(long_draft), theme="ios_teal")
    if counted != len(pages):
        FAILS.append(f"счётчик экранов врёт: обещал {counted}, нарисовано {len(pages)}")


def main() -> int:
    pic = sample_photo()
    check("мем", lambda: memegen.make_meme(pic, "верх", "низ"))
    check("демотиватор", lambda: memegen.make_demotivator(pic, "подпись", "мелкая"))
    check("классика", lambda: memegen.make_classic_meme(pic, "верх", "низ"))
    check("эмодзи-кластеры", lambda: [
        chatgen.render_emoji(c, 64) for c in ("😁", "🇷🇺", "1️⃣", "👨‍💻")])
    asyncio.run(run())

    if FAILS:
        print(f"ПРОВАЛЕНО ({len(FAILS)}):")
        for f in FAILS:
            print("  -", f)
        return 1
    print("всё собирается")
    return 0


if __name__ == "__main__":
    sys.exit(main())
