"""
Постироничный мем-бот.
Кидаешь фото -> получаешь фото с максимально всратой надписью Impact-стилем.
Плюс кнопки: "Добавить фразу" (любой юзер пополняет базу),
"Свой мем" (юзер сам загружает фото и сам пишет текст),
"Попробуй ещё" (новая случайная надпись на последнее фото),
"В предложку" (мгновенно предложить уже готовый мем — картинка с текстом
остаётся как есть, отдельно спрашивается только подпись к посту, можно
пустую) и "Предложить в канал" (тот же путь вручную: свои фото + подпись).
Предложка — ручная модерация: админ видит заявку с кнопками
Одобрить/Отклонить/Изменить текст, посты уходят в канал с подписью как
обычный пост, без наложения текста на картинку.
Вообще ВСЕ действия в боте доступны только подписчикам канала (CHANNEL_ID).

Запуск:
    export BOT_TOKEN="токен_от_BotFather"
    python bot.py
"""

import asyncio
import logging
import os
import re
import uuid

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from phrasebank import get_random_phrase, parse_phrase, add_phrase, load_phrases
from memegen import make_meme, make_classic_meme, make_demotivator, FONT_CHOICES_BY_ID
import stats
import submission_queue
import chatflow
import stickers
import phrase_queue

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()  # например @my_channel или -100...

dp = Dispatcher(storage=MemoryStorage())


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """Проверяет подписку на канал. Если канал ещё не настроен — не блокируем."""
    if not CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        logger.exception("Failed to check channel subscription")
        return False


def subscribe_kb() -> InlineKeyboardMarkup:
    channel_url = f"https://t.me/{CHANNEL_ID.lstrip('@')}" if CHANNEL_ID else "https://t.me"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Подписаться на канал", url=channel_url)],
            [InlineKeyboardButton(text="✅ Проверить", callback_data="check_sub")],
        ]
    )


class StatsMiddleware(BaseMiddleware):
    """Отмечает чат как активный в текущем месяце на любое сообщение."""

    async def __call__(self, handler, event: Message, data):
        if event.chat:
            try:
                stats.track(event.chat.id)
            except Exception:
                logger.exception("Failed to track stats")
        return await handler(event, data)


dp.message.middleware(StatsMiddleware())


class SubscriptionGateMiddleware(BaseMiddleware):
    """Блокирует вообще любое действие в боте, пока юзер не подписан на канал.
    Саму кнопку «Проверить» (check_sub) всегда пропускает — иначе юзер
    не сможет подтвердить подписку."""

    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        user = event.from_user
        bot: Bot = data["bot"]
        if user and not await is_subscribed(bot, user.id):
            text = (
                "чтобы пользоваться ботом, нужно быть подписанным на канал.\n"
                "подпишись и нажми «Проверить»"
            )
            if isinstance(event, CallbackQuery):
                await event.answer()
                await event.message.answer(text, reply_markup=subscribe_kb())
            else:
                await event.answer(text, reply_markup=subscribe_kb())
            return  # дальше хендлер не пускаем

        return await handler(event, data)

dp.message.middleware(SubscriptionGateMiddleware())
dp.callback_query.middleware(SubscriptionGateMiddleware())

BTN_ADD_PHRASE = "✍️ Добавить фразу"
BTN_CUSTOM_MEME = "🖼 Свой мем"
BTN_CHAT = chatflow.BTN_CHAT
BTN_PACKS = stickers.BTN_PACKS
BTN_SUBMIT = "📮 Предложить в канал"
BTN_HELP = "❓ Помощь"
BTN_CANCEL = "✖️ Отмена"
BTN_TRY_AGAIN = "🔁 Попробуй ещё"

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ADD_PHRASE), KeyboardButton(text=BTN_CUSTOM_MEME)],
        [KeyboardButton(text=BTN_CHAT)],
        [KeyboardButton(text=BTN_SUBMIT), KeyboardButton(text=BTN_PACKS)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)


BTN_SUBMIT_THIS = "📮 В предложку"
BTN_SUBMITTED = "✅ Отправлено"

MEME_CAPTION = "мем-машина без вкуса и совести: @randomem_bot"


RENDER_HISTORY_LIMIT = 20  # сколько последних мемов на чат помним для кнопок под старыми сообщениями


def new_render_id() -> str:
    return uuid.uuid4().hex[:10]


def remember_render(data: dict, render_id: str, entry: dict) -> dict:
    """
    Кладёт рендер в data["renders"] под своим render_id, а не в общий слот —
    иначе кнопка под старым мемом после следующей генерации подхватывала бы
    уже новый file_id (баг: в предложку уходил последний сгенерированный мем,
    а не тот, под которым нажали кнопку). Хранит последние RENDER_HISTORY_LIMIT
    рендеров на чат, чтобы data не росла бесконечно.
    """
    renders = dict(data.get("renders", {}))
    renders[render_id] = entry
    if len(renders) > RENDER_HISTORY_LIMIT:
        for old_id in list(renders.keys())[:-RENDER_HISTORY_LIMIT]:
            del renders[old_id]
    return renders


async def reset_state(state: FSMContext) -> None:
    """
    Как state.clear(), но не роняет data["renders"] — иначе после того как
    сценарий (предложка/своя фраза/отмена) завершается через state.clear(),
    кнопки "В предложку"/"Попробуй ещё" под остальными, ранее присланными
    мемами в этом чате переставали находить свой file_id.
    """
    data = await state.get_data()
    renders = data.get("renders")
    await state.clear()
    if renders:
        await state.update_data(renders=renders)


def try_again_kb(render_id: str, submitted: bool = False) -> InlineKeyboardMarkup:
    submit_btn = (
        InlineKeyboardButton(text=BTN_SUBMITTED, callback_data="noop")
        if submitted
        else InlineKeyboardButton(text=BTN_SUBMIT_THIS, callback_data=f"submit_last:{render_id}")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_TRY_AGAIN, callback_data=f"try_again:{render_id}"),
             submit_btn],
            [stickers.sticker_btn()],
        ]
    )


def submit_this_kb(render_id: str, submitted: bool = False) -> InlineKeyboardMarkup:
    submit_btn = (
        InlineKeyboardButton(text=BTN_SUBMITTED, callback_data="noop")
        if submitted
        else InlineKeyboardButton(text=BTN_SUBMIT_THIS, callback_data=f"submit_custom:{render_id}")
    )
    return InlineKeyboardMarkup(inline_keyboard=[[submit_btn], [stickers.sticker_btn()]])


def custom_format_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖼 Мем (текст сверху/снизу)", callback_data="custom_format:meme")],
            [InlineKeyboardButton(text="🎬 Демотиватор", callback_data="custom_format:demotivator")],
        ]
    )


def custom_font_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=choice["label"], callback_data=f"custom_font:{font_id}")]
        for font_id, choice in FONT_CHOICES_BY_ID.items()
    ]
    rows.append([InlineKeyboardButton(text="🎲 Любой (рандом)", callback_data="custom_font:random")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_review_kb(sub_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{sub_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{sub_id}"),
            ],
            [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"edit:{sub_id}")],
        ]
    )


def build_admin_caption(sub_id: str, text: str) -> str:
    return f"Заявка #{sub_id}\nТекст: {text or '(без текста)'}"


async def notify_admin_submission(bot: Bot, sub_id: str, text: str, file_id: str) -> None:
    if not ADMIN_ID:
        return
    try:
        sent = await bot.send_photo(
            ADMIN_ID, file_id, caption=build_admin_caption(sub_id, text), reply_markup=build_review_kb(sub_id)
        )
        submission_queue.set_admin_message_id(sub_id, sent.message_id)
    except Exception:
        logger.exception("Failed to notify admin about submission")


def build_phrase_review_kb(sub_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_phrase:{sub_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_phrase:{sub_id}"),
            ],
        ]
    )


def build_phrase_admin_caption(sub_id: str, text: str) -> str:
    return f"Новая фраза #{sub_id}\n{text}"


async def notify_admin_phrase_submission(bot: Bot, sub_id: str, text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        sent = await bot.send_message(
            ADMIN_ID,
            build_phrase_admin_caption(sub_id, text),
            reply_markup=build_phrase_review_kb(sub_id),
        )
        phrase_queue.set_admin_message_id(sub_id, sent.message_id)
    except Exception:
        logger.exception("Failed to notify admin about phrase submission")


def _phrase_key(text: str) -> str:
    """Форма для сравнения фраз: регистр, ё/е, пунктуация и пробелы не различаются."""
    s = text.casefold().replace("ё", "е")
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


async def offer_custom_text_as_phrase(bot: Bot, text: str, chat_id: int) -> None:
    """Текст, который человек придумал для своего мема, отправляем админу на
    модерацию как кандидата в общую базу — база так пополняется живыми фразами
    сама. Именно на модерацию, а не сразу в базу: иначе это та же дыра, из-за
    которой кнопку «Добавить фразу» в своё время закрыли проверкой."""
    if not ADMIN_ID or not text:
        return
    key = _phrase_key(text)
    if not key or phrase_queue.has_pending(text):
        return
    if any(_phrase_key(p) == key for p in load_phrases()):
        return
    try:
        sub_id = phrase_queue.add_submission(text, chat_id)
        await notify_admin_phrase_submission(bot, sub_id, f"(из своего мема) {text}")
    except Exception:
        logger.exception("Failed to queue custom meme text as phrase")


class MemeStates(StatesGroup):
    waiting_phrase = State()          # ждём текст новой фразы для базы
    waiting_custom_photo = State()    # ждём фото для своего мема
    waiting_custom_format = State()   # ждём выбор формата: мем/демотиватор
    waiting_custom_font = State()     # ждём выбор шрифта (только для формата "мем")
    waiting_custom_text = State()     # ждём текст для своего мема
    waiting_submit_photo = State()    # ждём фото для предложки
    waiting_submit_text = State()     # ждём текст (или "-") для предложки


class AdminStates(StatesGroup):
    editing_text = State()            # ждём от админа новый текст для заявки


def build_help_text() -> str:
    count = stats.monthly_active_count()
    users_line = f"\nботом пользуются ~{count} человек в этом месяце\n" if count >= 5 else ""
    return (
        "скинь фото просто так (без кнопок) — получишь случайный мем со случайной надписью.\n"
        f"{users_line}\n"
        "кнопки:\n"
        f"{BTN_ADD_PHRASE} — добавить свою фразу в общую базу\n"
        f"{BTN_CUSTOM_MEME} — загрузить своё фото и самому написать для него текст "
        "(это не рандомный мем — надпись придумываешь ты)\n"
        f"{BTN_CHAT} — собрать скриншот переписки: имена, аватарки, реплики, "
        "голосовые, кружки, стикеры. бот ведёт по шагам, ничего писать "
        "спецсимволами не надо\n"
        f"{BTN_SUBMIT} — предложить мем в канал (после ручной проверки)\n"
        f"{BTN_PACKS} — свои стикерпаки: под каждым мемом есть кнопка "
        "«в стикеры», можно и просто накидать своих картинок\n\n"
        "команды (для тех кто любит текстом):\n"
        "/add текст — то же самое что кнопка, но одним сообщением\n"
        "/reset — сбросить очередь показанных фраз для этого чата"
    )


# ---------- старт и помощь ----------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(build_help_text(), reply_markup=main_kb)


@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    await message.answer(build_help_text(), reply_markup=main_kb)


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
    await reset_state(state)
    await message.answer("отменил. можно продолжать как обычно.", reply_markup=main_kb)


# ---------- добавление фразы через кнопку ----------

@dp.message(F.text == BTN_ADD_PHRASE)
async def add_phrase_start(message: Message, state: FSMContext) -> None:
    await state.set_state(MemeStates.waiting_phrase)
    await message.answer(
        "пришли текст фразы, которую добавить в базу.\n"
        "можно с | чтобы разделить на верх/низ, например:\n"
        "я узнал|что бот теперь умнее меня\n\n"
        "без | вся фраза пойдёт вниз мема.\n"
        "фраза уйдёт на модерацию админу.",
        reply_markup=cancel_kb,
    )


@dp.message(Command("add"))
async def cmd_add(message: Message, bot: Bot) -> None:
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer(
            "после /add напиши саму фразу. можно с | для верх/низ, например:\n"
            "/add я узнал|что бот теперь умнее меня"
        )
        return
    sub_id = phrase_queue.add_submission(text, message.chat.id)
    await notify_admin_phrase_submission(bot, sub_id, text)
    await message.answer("отправил на модерацию, спасибо! если одобрят — попадёт в базу")


@dp.message(MemeStates.waiting_phrase, F.text)
async def add_phrase_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    text = message.text.strip()
    if not text:
        await message.answer("это не похоже на текст фразы, попробуй ещё раз")
        return
    sub_id = phrase_queue.add_submission(text, message.chat.id)
    await notify_admin_phrase_submission(bot, sub_id, text)
    await reset_state(state)
    await message.answer("отправил на модерацию, спасибо! если одобрят — попадёт в базу", reply_markup=main_kb)


# ---------- свой мем: фото + свой текст ----------

@dp.message(F.text == BTN_CUSTOM_MEME)
async def custom_meme_start(message: Message, state: FSMContext) -> None:
    await state.set_state(MemeStates.waiting_custom_photo)
    await message.answer(
        "здесь текст на мем придумываешь ты сам — пришли фото, а потом свой текст к нему.\n\n"
        "если нужен рандомный мем со случайной надписью — жми «✖️ Отмена» "
        "и просто скинь фото без этой кнопки, мем придёт сразу.",
        reply_markup=cancel_kb,
    )


@dp.message(MemeStates.waiting_custom_photo, F.photo)
async def custom_meme_got_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    await state.update_data(custom_photo_file_id=photo.file_id)
    await state.set_state(MemeStates.waiting_custom_format)
    await message.answer("фото принял. какой формат нужен?", reply_markup=custom_format_kb())


@dp.message(MemeStates.waiting_custom_photo)
async def custom_meme_wrong_input(message: Message) -> None:
    await message.answer("жду именно фото. пришли картинку, или нажми «✖️ Отмена»")


@dp.callback_query(F.data.startswith("custom_format:"), MemeStates.waiting_custom_format)
async def custom_format_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    fmt = callback.data.split(":", 1)[1]
    await state.update_data(custom_format=fmt)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if fmt == "demotivator":
        await state.set_state(MemeStates.waiting_custom_text)
        await callback.message.answer(
            "теперь напиши подпись для демотиватора.\n"
            "можно с | чтобы добавить мелкую строку под основной подписью, например:\n"
            "основная подпись|мелкая подстрочная строка\n\n"
            "без | будет только основная подпись.",
            reply_markup=cancel_kb,
        )
    else:
        await state.set_state(MemeStates.waiting_custom_font)
        await callback.message.answer("выбери шрифт для надписи:", reply_markup=custom_font_kb())


@dp.callback_query(F.data.startswith("custom_font:"), MemeStates.waiting_custom_font)
async def custom_font_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    font_id = callback.data.split(":", 1)[1]
    await state.update_data(custom_font_id=None if font_id == "random" else font_id)
    await state.set_state(MemeStates.waiting_custom_text)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "теперь напиши текст для мема.\n"
        "можно с | чтобы разделить на верх/низ, например:\n"
        "верхний текст|нижний текст\n\n"
        "без | весь текст встанет снизу.",
        reply_markup=cancel_kb,
    )


@dp.message(MemeStates.waiting_custom_text, F.text)
async def custom_meme_got_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    file_id = data.get("custom_photo_file_id")
    if not file_id:
        await reset_state(state)
        await message.answer("что-то потерялось, давай заново", reply_markup=main_kb)
        return

    fmt = data.get("custom_format", "meme")
    top, bottom = parse_phrase(message.text.strip())

    file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    try:
        if fmt == "demotivator":
            if top:
                caption, subtitle = top, bottom
            else:
                caption, subtitle = bottom, ""
            meme_buf = make_demotivator(image_bytes, caption, subtitle)
        else:
            font_id = data.get("custom_font_id")
            font_choice = FONT_CHOICES_BY_ID.get(font_id) if font_id else None
            meme_buf = make_classic_meme(image_bytes, top, bottom or "", font_choice=font_choice)
    except Exception:
        logger.exception("Failed to render custom meme")
        await message.answer("не получилось собрать мем, но это тоже часть постиронии", reply_markup=main_kb)
        await reset_state(state)
        return

    await state.set_state(None)
    render_id = new_render_id()
    sent = await message.answer_photo(
        BufferedInputFile(meme_buf.read(), filename="meme.jpg"),
        caption=MEME_CAPTION,
        reply_markup=submit_this_kb(render_id),
    )
    renders = remember_render(data, render_id, {"rendered_file_id": sent.photo[-1].file_id})
    await state.update_data(renders=renders)

    await offer_custom_text_as_phrase(bot, message.text.strip(), message.chat.id)


# ---------- обычный режим: просто прислали фото -> случайный мем ----------

@dp.message(StateFilter(None), F.photo)
async def handle_photo(message: Message, state: FSMContext, bot: Bot) -> None:
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

    render_id = new_render_id()
    sent = await message.answer_photo(
        BufferedInputFile(meme_buf.read(), filename="meme.jpg"),
        caption=MEME_CAPTION,
        reply_markup=try_again_kb(render_id),
    )
    data = await state.get_data()
    renders = remember_render(
        data, render_id, {"source_file_id": photo.file_id, "rendered_file_id": sent.photo[-1].file_id}
    )
    await state.update_data(renders=renders)


@dp.message(StateFilter(None), F.document & F.document.mime_type.startswith("image/"))
async def handle_document_photo(message: Message, state: FSMContext, bot: Bot) -> None:
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

    render_id = new_render_id()
    sent = await message.answer_photo(
        BufferedInputFile(meme_buf.read(), filename="meme.jpg"),
        caption=MEME_CAPTION,
        reply_markup=try_again_kb(render_id),
    )
    data = await state.get_data()
    renders = remember_render(
        data, render_id, {"source_file_id": doc.file_id, "rendered_file_id": sent.photo[-1].file_id}
    )
    await state.update_data(renders=renders)


@dp.callback_query(F.data.startswith("try_again:"))
async def try_again(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    render_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    entry = data.get("renders", {}).get(render_id)
    if not entry:
        await callback.answer("не нашёл предыдущее фото, кинь новое", show_alert=True)
        return
    file_id = entry["source_file_id"]

    await callback.answer()

    file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    phrase = get_random_phrase(callback.message.chat.id)
    top, bottom = parse_phrase(phrase)

    try:
        meme_buf = make_meme(image_bytes, top, bottom or "")
    except Exception:
        logger.exception("Failed to render meme (try again)")
        await callback.message.answer("что-то пошло не так, но это тоже часть постиронии")
        return

    new_id = new_render_id()
    sent = await callback.message.answer_photo(
        BufferedInputFile(meme_buf.read(), filename="meme.jpg"),
        caption=MEME_CAPTION,
        reply_markup=try_again_kb(new_id),
    )
    renders = remember_render(
        data, new_id, {"source_file_id": file_id, "rendered_file_id": sent.photo[-1].file_id}
    )
    await state.update_data(renders=renders)


@dp.callback_query(F.data.startswith("submit_last:"))
async def submit_last(callback: CallbackQuery, state: FSMContext) -> None:
    render_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    entry = data.get("renders", {}).get(render_id)
    if not entry:
        await callback.answer("не нашёл мем, кинь фото заново", show_alert=True)
        return
    rendered_file_id = entry["rendered_file_id"]

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=try_again_kb(render_id, submitted=True))
    except Exception:
        pass

    await state.update_data(submit_photo_file_id=rendered_file_id)
    await state.set_state(MemeStates.waiting_submit_text)
    await callback.message.answer(
        "теперь пришли текст для подписи к посту,\n"
        "или отправь просто «-», если подпись не нужна — мем уйдёт как есть",
        reply_markup=cancel_kb,
    )


@dp.callback_query(F.data.startswith("submit_custom:"))
async def submit_custom(callback: CallbackQuery, state: FSMContext) -> None:
    render_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    entry = data.get("renders", {}).get(render_id)
    if not entry:
        await callback.answer("не нашёл мем, загрузи заново", show_alert=True)
        return
    rendered_file_id = entry["rendered_file_id"]

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=submit_this_kb(render_id, submitted=True))
    except Exception:
        pass

    await state.update_data(submit_photo_file_id=rendered_file_id)
    await state.set_state(MemeStates.waiting_submit_text)
    await callback.message.answer(
        "теперь пришли текст для подписи к посту,\n"
        "или отправь просто «-», если подпись не нужна — мем уйдёт как есть",
        reply_markup=cancel_kb,
    )


@dp.callback_query(F.data == "noop")
async def noop_cb(callback: CallbackQuery) -> None:
    await callback.answer("уже отправлено")


# ---------- предложка: фото (+ опционально текст) на модерацию ----------


@dp.message(F.text == BTN_SUBMIT)
async def submit_start(message: Message, state: FSMContext) -> None:
    await state.set_state(MemeStates.waiting_submit_photo)
    await message.answer(
        "пришли фото, которое хочешь предложить в канал",
        reply_markup=cancel_kb,
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, bot: Bot) -> None:
    if await is_subscribed(bot, callback.from_user.id):
        await callback.answer("подписка подтверждена!")
        await callback.message.answer(
            "отлично, теперь можно пользоваться ботом:",
            reply_markup=main_kb,
        )
    else:
        await callback.answer("пока не вижу подписку, попробуй ещё раз через пару секунд", show_alert=True)


@dp.message(MemeStates.waiting_submit_photo, F.photo)
async def submit_got_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    await state.update_data(submit_photo_file_id=photo.file_id)
    await state.set_state(MemeStates.waiting_submit_text)
    await message.answer(
        "теперь пришли текст для мема (можно с | для верх/низ),\n"
        "или отправь просто «-», если текст не нужен — фото уйдёт как есть",
        reply_markup=cancel_kb,
    )


@dp.message(MemeStates.waiting_submit_photo)
async def submit_wrong_input(message: Message) -> None:
    await message.answer("жду именно фото. пришли картинку, или нажми «✖️ Отмена»")


@dp.message(MemeStates.waiting_submit_text, F.text)
async def submit_got_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    file_id = data.get("submit_photo_file_id")
    if not file_id:
        await reset_state(state)
        await message.answer("что-то потерялось, давай заново", reply_markup=main_kb)
        return

    text = message.text.strip()
    if text == "-":
        text = ""

    sub_id = submission_queue.add_submission(file_id, text, message.chat.id)
    await reset_state(state)
    await message.answer(
        "отправил на модерацию, спасибо! если одобрят — попадёт в канал",
        reply_markup=main_kb,
    )

    await notify_admin_submission(bot, sub_id, text, file_id)


@dp.callback_query(F.data.startswith("approve:"))
async def approve_submission(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("только админ может это делать", show_alert=True)
        return

    sub_id = callback.data.split(":", 1)[1]
    sub = submission_queue.get_submission(sub_id)
    if not sub or sub.get("status") != "pending":
        await callback.answer("уже обработано", show_alert=True)
        return

    if not CHANNEL_ID:
        await callback.answer("канал не настроен (переменная CHANNEL_ID)", show_alert=True)
        return

    try:
        if sub["text"]:
            await bot.send_photo(CHANNEL_ID, sub["photo_file_id"], caption=sub["text"])
        else:
            await bot.send_photo(CHANNEL_ID, sub["photo_file_id"])
    except Exception:
        logger.exception("Failed to post submission to channel")
        await callback.answer("не получилось запостить в канал", show_alert=True)
        return

    submission_queue.set_status(sub_id, "approved")
    await callback.answer("опубликовано")
    try:
        await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n✅ ОДОБРЕНО")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("reject:"))
async def reject_submission(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("только админ может это делать", show_alert=True)
        return

    sub_id = callback.data.split(":", 1)[1]
    sub = submission_queue.get_submission(sub_id)
    if not sub or sub.get("status") != "pending":
        await callback.answer("уже обработано", show_alert=True)
        return

    submission_queue.set_status(sub_id, "rejected")
    await callback.answer("отклонено")
    try:
        await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n❌ ОТКЛОНЕНО")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("edit:"))
async def edit_submission_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("только админ может это делать", show_alert=True)
        return

    sub_id = callback.data.split(":", 1)[1]
    sub = submission_queue.get_submission(sub_id)
    if not sub or sub.get("status") != "pending":
        await callback.answer("уже обработано", show_alert=True)
        return

    await state.update_data(editing_sub_id=sub_id)
    await state.set_state(AdminStates.editing_text)
    await callback.answer()
    await callback.message.answer(
        f"пришли новый текст подписи для заявки #{sub_id} (или «-» чтобы убрать подпись)"
    )


@dp.message(AdminStates.editing_text, F.text)
async def edit_submission_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    sub_id = data.get("editing_sub_id")
    await reset_state(state)
    if not sub_id:
        return

    sub = submission_queue.get_submission(sub_id)
    if not sub or sub.get("status") != "pending":
        await message.answer("заявка уже обработана")
        return

    new_text = message.text.strip()
    if new_text == "-":
        new_text = ""
    submission_queue.set_text(sub_id, new_text)
    await message.answer(f"текст заявки #{sub_id} обновлён")

    admin_message_id = sub.get("admin_message_id")
    if admin_message_id:
        try:
            await bot.edit_message_caption(
                chat_id=ADMIN_ID,
                message_id=admin_message_id,
                caption=build_admin_caption(sub_id, new_text),
                reply_markup=build_review_kb(sub_id),
            )
        except Exception:
            logger.exception("Failed to update admin caption after edit")


@dp.callback_query(F.data.startswith("approve_phrase:"))
async def approve_phrase_submission(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("только админ может это делать", show_alert=True)
        return

    sub_id = callback.data.split(":", 1)[1]
    sub = phrase_queue.get_submission(sub_id)
    if not sub or sub.get("status") != "pending":
        await callback.answer("уже обработано", show_alert=True)
        return

    add_phrase(sub["text"])
    phrase_queue.set_status(sub_id, "approved")
    await callback.answer("добавлено в базу")
    try:
        await callback.message.edit_text(callback.message.text + "\n\n✅ ОДОБРЕНО")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("reject_phrase:"))
async def reject_phrase_submission(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("только админ может это делать", show_alert=True)
        return

    sub_id = callback.data.split(":", 1)[1]
    sub = phrase_queue.get_submission(sub_id)
    if not sub or sub.get("status") != "pending":
        await callback.answer("уже обработано", show_alert=True)
        return

    phrase_queue.set_status(sub_id, "rejected")
    await callback.answer("отклонено")
    try:
        await callback.message.edit_text(callback.message.text + "\n\n❌ ОТКЛОНЕНО")
    except Exception:
        pass


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Сделай: export BOT_TOKEN='твой_токен_от_BotFather'")

    dp.include_router(chatflow.router)
    dp.include_router(stickers.router)
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
