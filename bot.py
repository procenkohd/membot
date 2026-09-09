"""
Постироничный мем-бот.
Кидаешь фото -> получаешь фото с максимально всратой надписью Impact-стилем.
Плюс кнопки: "Добавить фразу" (любой юзер пополняет базу) и
"Свой мем" (юзер сам загружает фото и сам пишет текст).

Запуск:
    export BOT_TOKEN="токен_от_BotFather"
    python bot.py
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    BufferedInputFile,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

from phrasebank import get_random_phrase, parse_phrase, add_phrase
from memegen import make_meme

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

dp = Dispatcher(storage=MemoryStorage())

BTN_ADD_PHRASE = "✍️ Добавить фразу"
BTN_CUSTOM_MEME = "🖼 Свой мем"
BTN_HELP = "❓ Помощь"
BTN_CANCEL = "✖️ Отмена"

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ADD_PHRASE), KeyboardButton(text=BTN_CUSTOM_MEME)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)


class MemeStates(StatesGroup):
    waiting_phrase = State()          # ждём текст новой фразы для базы
    waiting_custom_photo = State()    # ждём фото для своего мема
    waiting_custom_text = State()     # ждём текст для своего мема


HELP_TEXT = (
    "скинь фото — получишь случайный мем.\n\n"
    "кнопки:\n"
    f"{BTN_ADD_PHRASE} — добавить свою фразу в общую базу\n"
    f"{BTN_CUSTOM_MEME} — загрузить своё фото и написать текст самому\n\n"
    "команды (для тех кто любит текстом):\n"
    "/add текст — то же самое что кнопка, но одним сообщением\n"
    "/reset — сбросить очередь показанных фраз для этого чата"
)


# ---------- старт и помощь ----------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=main_kb)


@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_kb)


@dp.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    from phrasebank import _load_state, _save_state
    state = _load_state()
    state.pop(str(message.chat.id), None)
    _save_state(state)
    await message.answer("колода фраз для этого чата сброшена")


# ---------- отмена (работает в любом состоянии) ----------

@dp.message(F.text == BTN_CANCEL)
async def cancel_any(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("отменил. можно продолжать как обычно.", reply_markup=main_kb)


# ---------- добавление фразы через кнопку ----------

@dp.message(F.text == BTN_ADD_PHRASE)
async def add_phrase_start(message: Message, state: FSMContext) -> None:
    await state.set_state(MemeStates.waiting_phrase)
    await message.answer(
        "пришли текст фразы, которую добавить в базу.\n"
        "можно с | чтобы разделить на верх/низ, например:\n"
        "я узнал|что бот теперь умнее меня\n\n"
        "без | вся фраза пойдёт вниз мема.",
        reply_markup=cancel_kb,
    )


@dp.message(Command("add"))
async def cmd_add(message: Message) -> None:
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer(
            "после /add напиши саму фразу. можно с | для верх/низ, например:\n"
            "/add я узнал|что бот теперь умнее меня"
        )
        return
    add_phrase(text)
    await message.answer("добавил в базу, спасибо")


@dp.message(MemeStates.waiting_phrase, F.text)
async def add_phrase_finish(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text:
        await message.answer("это не похоже на текст фразы, попробуй ещё раз")
        return
    add_phrase(text)
    await state.clear()
    await message.answer("добавил в базу, спасибо", reply_markup=main_kb)


# ---------- свой мем: фото + свой текст ----------

@dp.message(F.text == BTN_CUSTOM_MEME)
async def custom_meme_start(message: Message, state: FSMContext) -> None:
    await state.set_state(MemeStates.waiting_custom_photo)
    await message.answer("пришли фото, на котором сделать мем", reply_markup=cancel_kb)


@dp.message(MemeStates.waiting_custom_photo, F.photo)
async def custom_meme_got_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    await state.update_data(custom_photo_file_id=photo.file_id)
    await state.set_state(MemeStates.waiting_custom_text)
    await message.answer(
        "теперь напиши текст для мема.\n"
        "можно с | чтобы разделить на верх/низ, например:\n"
        "верхний текст|нижний текст\n\n"
        "без | весь текст встанет снизу.",
        reply_markup=cancel_kb,
    )


@dp.message(MemeStates.waiting_custom_photo)
async def custom_meme_wrong_input(message: Message) -> None:
    await message.answer("жду именно фото. пришли картинку, или нажми «✖️ Отмена»")


@dp.message(MemeStates.waiting_custom_text, F.text)
async def custom_meme_got_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    file_id = data.get("custom_photo_file_id")
    if not file_id:
        await state.clear()
        await message.answer("что-то потерялось, давай заново", reply_markup=main_kb)
        return

    top, bottom = parse_phrase(message.text.strip())

    file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    try:
        meme_buf = make_meme(image_bytes, top, bottom or "")
    except Exception:
        logger.exception("Failed to render custom meme")
        await message.answer("не получилось собрать мем, но это тоже часть постиронии", reply_markup=main_kb)
        await state.clear()
        return

    await state.clear()
    await message.answer_photo(
        BufferedInputFile(meme_buf.read(), filename="meme.jpg"),
        reply_markup=main_kb,
    )


# ---------- обычный режим: просто прислали фото -> случайный мем ----------

@dp.message(StateFilter(None), F.photo)
async def handle_photo(message: Message, bot: Bot) -> None:
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    phrase = get_random_phrase(message.chat.id)
    top, bottom = parse_phrase(phrase)

    try:
        meme_buf = make_meme(image_bytes, top, bottom or "")
    except Exception:
        logger.exception("Failed to render meme")
        await message.answer("что-то пошло не так при генерации, но это тоже часть постиронии")
        return

    await message.answer_photo(
        BufferedInputFile(meme_buf.read(), filename="meme.jpg"),
    )


@dp.message(StateFilter(None), F.document & F.document.mime_type.startswith("image/"))
async def handle_document_photo(message: Message, bot: Bot) -> None:
    # если фото прислали "файлом", без сжатия
    doc = message.document
    file = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    phrase = get_random_phrase(message.chat.id)
    top, bottom = parse_phrase(phrase)

    try:
        meme_buf = make_meme(image_bytes, top, bottom or "")
    except Exception:
        logger.exception("Failed to render meme")
        await message.answer("что-то пошло не так при генерации, но это тоже часть постиронии")
        return

    await message.answer_photo(
        BufferedInputFile(meme_buf.read(), filename="meme.jpg"),
    )


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Сделай: export BOT_TOKEN='твой_токен_от_BotFather'")

    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
