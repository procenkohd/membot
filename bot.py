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
from phrase_categories import ALL, ABSURD, DECK_CATEGORIES, HARD, INTELLECTUAL
from memegen import (
    CUSTOM_FONT_IDS,
    FONT_CHOICES_BY_ID,
    make_classic_meme,
    make_demotivator,
    make_meme,
)
import stats
import submission_queue
import chatflow
import stickers
import ui
import phrase_queue
import render_store
from sqlite_storage import SQLiteStorage
from storage import data_path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()  # обратная совместимость
SUBSCRIPTION_CHANNEL_ID = os.environ.get("SUBSCRIPTION_CHANNEL_ID", CHANNEL_ID).strip()
POST_CHANNEL_ID = os.environ.get("POST_CHANNEL_ID", CHANNEL_ID).strip()
CHANNEL_URL = os.environ.get("CHANNEL_URL", "").strip()

dp = Dispatcher(storage=SQLiteStorage(data_path("fsm.sqlite3")))

MAX_MEME_TEXT = 500
MAX_PHRASE_TEXT = 500
MAX_SUBMISSION_TEXT = 900


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """Проверяет подписку на канал. Если канал ещё не настроен — не блокируем."""
    if not SUBSCRIPTION_CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(SUBSCRIPTION_CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        logger.exception("Failed to check channel subscription")
        return False


def subscribe_kb() -> InlineKeyboardMarkup:
    channel_url = CHANNEL_URL
    if not channel_url and SUBSCRIPTION_CHANNEL_ID.startswith("@"):
        channel_url = f"https://t.me/{SUBSCRIPTION_CHANNEL_ID.lstrip('@')}"
    rows = []
    if channel_url:
        rows.append([InlineKeyboardButton(text="➡️ Подписаться на канал", url=channel_url)])
    rows.append([InlineKeyboardButton(text="✅ Проверить", callback_data="check_sub")])
    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


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

        if user:
            try:
                stats.track(user.id)
            except Exception:
                logger.exception("Failed to track stats")

        return await handler(event, data)

dp.message.middleware(SubscriptionGateMiddleware())
dp.callback_query.middleware(SubscriptionGateMiddleware())

BTN_ADD_PHRASE = "✍️ Добавить фразу"
BTN_CUSTOM_MEME = "🖼 Свой мем"
BTN_CHAT = chatflow.BTN_CHAT
BTN_PACKS = stickers.BTN_PACKS
BTN_SUBMIT = "📮 Предложить в канал"
BTN_HELP = "❓ Помощь"
BTN_CANCEL = ui.BTN_CANCEL
BTN_REPIC = "🖼 Другая картинка"
BTN_ANOTHER_CAPTION = "🎲 Другая подпись"
BTN_MORE_ABSURD = "🤡 Сделай абсурднее"
BTN_HARDER = "☠️ Сделай жёстче"
BTN_SMARTER = "🧠 Сделай интеллектуальнее"

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ADD_PHRASE), KeyboardButton(text=BTN_CUSTOM_MEME)],
        [KeyboardButton(text=BTN_CHAT)],
        [KeyboardButton(text=BTN_SUBMIT), KeyboardButton(text=BTN_PACKS)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)

cancel_kb = ui.cancel_kb


BTN_SUBMIT_THIS = "📮 В предложку"
BTN_SUBMITTED = "✅ Отправлено"

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


def random_render_entry(source_file_id: str, rendered_file_id: str, phrase: str,
                        category: str = ALL) -> dict:
    """Единая форма записи: все кнопки старого мема получают и фото, и фразу."""
    return {
        "source_file_id": source_file_id,
        "rendered_file_id": rendered_file_id,
        "spec": {"kind": "random", "phrase": phrase, "category": category},
    }


async def persist_render(state: FSMContext, render_id: str, entry: dict,
                         chat_id: int, user_id: int) -> None:
    """Пишет данные кнопок отдельно от FSM и оставляет совместимую копию в нём."""
    render_store.save(render_id, chat_id, user_id, entry)
    data = await state.get_data()
    await state.update_data(renders=remember_render(data, render_id, entry))


async def find_render(callback: CallbackQuery, state: FSMContext, render_id: str) -> dict | None:
    """Ищет новый durable-рендер, затем переносит запись старого формата из FSM."""
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    entry = render_store.get(render_id, chat_id, user_id)
    if entry:
        return entry
    data = await state.get_data()
    entry = (data.get("renders") or {}).get(render_id)
    if entry:
        render_store.save(render_id, chat_id, user_id, entry)
    return entry


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
            [InlineKeyboardButton(text=BTN_ANOTHER_CAPTION,
                                  callback_data=f"reroll:{ALL}:{render_id}"),
             submit_btn],
            [InlineKeyboardButton(text=BTN_MORE_ABSURD,
                                  callback_data=f"reroll:{ABSURD}:{render_id}"),
             InlineKeyboardButton(text=BTN_HARDER,
                                  callback_data=f"reroll:{HARD}:{render_id}")],
            [InlineKeyboardButton(text=BTN_SMARTER,
                                  callback_data=f"reroll:{INTELLECTUAL}:{render_id}")],
            [InlineKeyboardButton(text=BTN_REPIC, callback_data=f"repic:{render_id}"),
             stickers.sticker_btn()],
        ]
    )


def submit_this_kb(render_id: str, submitted: bool = False) -> InlineKeyboardMarkup:
    submit_btn = (
        InlineKeyboardButton(text=BTN_SUBMITTED, callback_data="noop")
        if submitted
        else InlineKeyboardButton(text=BTN_SUBMIT_THIS, callback_data=f"submit_custom:{render_id}")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [submit_btn],
        [InlineKeyboardButton(text=BTN_REPIC, callback_data=f"repic:{render_id}"),
         stickers.sticker_btn()],
    ])


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
        for font_id in CUSTOM_FONT_IDS
        for choice in (FONT_CHOICES_BY_ID[font_id],)
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


def validate_text(text: str, limit: int, label: str = "текст") -> str | None:
    """Возвращает понятную ошибку вместо сиротской заявки или сломанного рендера."""
    if not text.strip():
        return f"{label} пустой"
    if len(text) > limit:
        return f"{label} слишком длинный: максимум {limit} символов, сейчас {len(text)}"
    return None


def moderation_error(require_channel: bool = False) -> str | None:
    if not ADMIN_ID:
        return "предложка пока не настроена: владелец бота не указал ADMIN_ID"
    if require_channel and not POST_CHANNEL_ID:
        return "канал для публикации пока не настроен"
    return None


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
    waiting_repic = State()           # ждём новую картинку под уже готовую надпись


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
        "под готовым мемом можно отдельно попросить другую, абсурдную, "
        "жёсткую или интеллектуальную подпись — картинка останется той же\n\n"
        "команды (для тех кто любит текстом):\n"
        "/add текст — то же самое что кнопка, но одним сообщением\n"
        "/reset — сбросить очередь показанных фраз для этого чата"
    )


# ---------- старт и помощь ----------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await reset_state(state)
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
    error = moderation_error()
    if error:
        await message.answer(error, reply_markup=main_kb)
        return
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
    error = moderation_error()
    if error:
        await message.answer(error)
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer(
            "после /add напиши саму фразу. можно с | для верх/низ, например:\n"
            "/add я узнал|что бот теперь умнее меня"
        )
        return
    error = validate_text(text, MAX_PHRASE_TEXT, "фраза")
    if error:
        await message.answer(error)
        return
    sub_id = phrase_queue.add_submission(text, message.chat.id)
    await notify_admin_phrase_submission(bot, sub_id, text)
    await message.answer("отправил на модерацию, спасибо! если одобрят — попадёт в базу")


@dp.message(MemeStates.waiting_phrase, F.text)
async def add_phrase_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    text = message.text.strip()
    error = validate_text(text, MAX_PHRASE_TEXT, "фраза")
    if error:
        await message.answer(error)
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

    text = message.text.strip()
    error = validate_text(text, MAX_MEME_TEXT)
    if error:
        await message.answer(error)
        return
    fmt = data.get("custom_format", "meme")
    top, bottom = parse_phrase(text)

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
        caption=await ui.bot_caption(bot),
    )
    await message.answer("можно кидать следующее фото", reply_markup=main_kb)
    entry = {
        "rendered_file_id": sent.photo[-1].file_id,
        # текст держим при рендере, чтобы «другая картинка» могла его повторить
        "spec": {"kind": "custom", "text": text, "fmt": fmt,
                 "font_id": data.get("custom_font_id")},
    }
    await persist_render(state, render_id, entry, message.chat.id, message.from_user.id)
    await sent.edit_reply_markup(reply_markup=submit_this_kb(render_id))

    await offer_custom_text_as_phrase(bot, text, message.chat.id)


# ---------- та же надпись на другой картинке ----------

def render_by_spec(image_bytes: bytes, spec: dict):
    """Пересобирает мем по сохранённой надписи. Случайная фраза рисуется как
    обычно, своя — тем же форматом и шрифтом, что выбрал юзер в первый раз."""
    if spec.get("kind") == "custom":
        top, bottom = parse_phrase(spec.get("text", ""))
        if spec.get("fmt") == "demotivator":
            caption, subtitle = (top, bottom) if top else (bottom, "")
            return make_demotivator(image_bytes, caption, subtitle)
        font_id = spec.get("font_id")
        font_choice = FONT_CHOICES_BY_ID.get(font_id) if font_id else None
        return make_classic_meme(image_bytes, top, bottom or "", font_choice=font_choice)
    top, bottom = parse_phrase(spec.get("phrase", ""))
    return make_meme(image_bytes, top, bottom or "")


@dp.callback_query(F.data.startswith("repic:"))
async def repic_ask(callback: CallbackQuery, state: FSMContext) -> None:
    render_id = callback.data.split(":", 1)[1]
    entry = await find_render(callback, state, render_id)
    spec = (entry or {}).get("spec")
    if not spec:
        await callback.answer("надпись этого мема я уже не помню, сделай новый",
                              show_alert=True)
        return
    await state.set_state(MemeStates.waiting_repic)
    await state.update_data(repic_spec=spec)
    await callback.answer()
    await callback.message.answer("кидай другую картинку — надпись оставлю ту же",
                                  reply_markup=cancel_kb)


@dp.message(MemeStates.waiting_repic, F.photo)
async def repic_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    spec = data.get("repic_spec")
    if not spec:
        await reset_state(state)
        await message.answer("что-то потерялось, давай заново", reply_markup=main_kb)
        return

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    image_bytes = (await bot.download_file(file.file_path)).read()
    try:
        meme_buf = render_by_spec(image_bytes, spec)
    except Exception:
        logger.exception("Failed to re-render meme with a new photo")
        await message.answer("не получилось собрать, попробуй другую картинку")
        return

    await state.set_state(None)
    render_id = new_render_id()
    kb = try_again_kb(render_id) if spec.get("kind") == "random" else submit_this_kb(render_id)
    sent = await message.answer_photo(
        BufferedInputFile(meme_buf.read(), filename="meme.jpg"),
        caption=await ui.bot_caption(bot))
    await message.answer("можно менять картинку дальше или кидать новое фото",
                         reply_markup=main_kb)
    entry = {
        "source_file_id": photo.file_id,
        "rendered_file_id": sent.photo[-1].file_id,
        "spec": spec,
    }
    await persist_render(state, render_id, entry, message.chat.id, message.from_user.id)
    await sent.edit_reply_markup(reply_markup=kb)


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
        caption=await ui.bot_caption(bot),
    )
    entry = random_render_entry(photo.file_id, sent.photo[-1].file_id, phrase)
    await persist_render(state, render_id, entry, message.chat.id, message.from_user.id)
    await sent.edit_reply_markup(reply_markup=try_again_kb(render_id))


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
        caption=await ui.bot_caption(bot),
    )
    entry = random_render_entry(doc.file_id, sent.photo[-1].file_id, phrase)
    await persist_render(state, render_id, entry, message.chat.id, message.from_user.id)
    await sent.edit_reply_markup(reply_markup=try_again_kb(render_id))


async def reroll_meme(callback: CallbackQuery, state: FSMContext, bot: Bot,
                      render_id: str, category: str) -> None:
    """Рисует новую фразу выбранной категории поверх исходной фотографии."""
    entry = await find_render(callback, state, render_id)
    if not entry:
        await callback.answer("не нашёл предыдущее фото, кинь новое", show_alert=True)
        return
    file_id = entry.get("source_file_id")
    if not file_id:
        await callback.answer("исходное фото не сохранилось, кинь его заново",
                              show_alert=True)
        return

    await callback.answer()

    try:
        file = await bot.get_file(file_id)
        file_bytes = await bot.download_file(file.file_path)
        image_bytes = file_bytes.read()
    except Exception:
        logger.exception("Failed to download source photo for reroll")
        await callback.message.answer("не получилось достать исходное фото, кинь его заново")
        return

    phrase = get_random_phrase(callback.message.chat.id, category)
    top, bottom = parse_phrase(phrase)

    try:
        meme_buf = make_meme(image_bytes, top, bottom or "")
    except Exception:
        logger.exception("Failed to render meme reroll for category %s", category)
        await callback.message.answer("что-то пошло не так, но это тоже часть постиронии")
        return

    new_id = new_render_id()
    sent = await callback.message.answer_photo(
        BufferedInputFile(meme_buf.read(), filename="meme.jpg"),
        caption=await ui.bot_caption(bot),
    )
    entry = random_render_entry(file_id, sent.photo[-1].file_id, phrase, category)
    await persist_render(
        state, new_id, entry, callback.message.chat.id, callback.from_user.id)
    await sent.edit_reply_markup(reply_markup=try_again_kb(new_id))


@dp.callback_query(F.data.startswith("reroll:"))
async def reroll_by_category(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    try:
        _, category, render_id = callback.data.split(":", 2)
    except ValueError:
        await callback.answer("кнопка сломалась, сделай новый мем", show_alert=True)
        return
    if category not in DECK_CATEGORIES:
        await callback.answer("не знаю такую категорию", show_alert=True)
        return
    await reroll_meme(callback, state, bot, render_id, category)


@dp.callback_query(F.data.startswith("try_again:"))
async def try_again(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Совместимость со старыми сообщениями, отправленными до новых кнопок."""
    render_id = callback.data.split(":", 1)[1]
    await reroll_meme(callback, state, bot, render_id, ALL)


@dp.callback_query(F.data.startswith("submit_last:"))
async def submit_last(callback: CallbackQuery, state: FSMContext) -> None:
    error = moderation_error(require_channel=True)
    if error:
        await callback.answer(error, show_alert=True)
        return
    render_id = callback.data.split(":", 1)[1]
    entry = await find_render(callback, state, render_id)
    if not entry:
        await callback.answer("не нашёл мем, кинь фото заново", show_alert=True)
        return
    rendered_file_id = entry["rendered_file_id"]

    await callback.answer()
    await state.update_data(
        submit_photo_file_id=rendered_file_id,
        submit_render_id=render_id,
        submit_kind="random",
        submit_message_id=callback.message.message_id,
    )
    await state.set_state(MemeStates.waiting_submit_text)
    await callback.message.answer(
        "теперь пришли текст для подписи к посту,\n"
        "или отправь просто «-», если подпись не нужна — мем уйдёт как есть",
        reply_markup=cancel_kb,
    )


@dp.callback_query(F.data.startswith("submit_custom:"))
async def submit_custom(callback: CallbackQuery, state: FSMContext) -> None:
    error = moderation_error(require_channel=True)
    if error:
        await callback.answer(error, show_alert=True)
        return
    render_id = callback.data.split(":", 1)[1]
    entry = await find_render(callback, state, render_id)
    if not entry:
        await callback.answer("не нашёл мем, загрузи заново", show_alert=True)
        return
    rendered_file_id = entry["rendered_file_id"]

    await callback.answer()
    await state.update_data(
        submit_photo_file_id=rendered_file_id,
        submit_render_id=render_id,
        submit_kind="custom",
        submit_message_id=callback.message.message_id,
    )
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
    error = moderation_error(require_channel=True)
    if error:
        await message.answer(error, reply_markup=main_kb)
        return
    await state.set_state(MemeStates.waiting_submit_photo)
    await message.answer(
        "пришли фото, которое хочешь предложить в канал",
        reply_markup=cancel_kb,
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, bot: Bot) -> None:
    if await is_subscribed(bot, callback.from_user.id):
        try:
            stats.track(callback.from_user.id)
        except Exception:
            logger.exception("Failed to track stats")
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
        "теперь пришли подпись к посту,\n"
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
    if len(text) > MAX_SUBMISSION_TEXT:
        await message.answer(
            f"подпись слишком длинная: максимум {MAX_SUBMISSION_TEXT} символов, "
            f"сейчас {len(text)}. сократи и пришли ещё раз"
        )
        return

    sub_id = submission_queue.add_submission(file_id, text, message.chat.id)
    render_id = data.get("submit_render_id")
    source_message_id = data.get("submit_message_id")
    if render_id and source_message_id:
        kb = (try_again_kb(render_id, submitted=True)
              if data.get("submit_kind") == "random"
              else submit_this_kb(render_id, submitted=True))
        try:
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=source_message_id, reply_markup=kb)
        except Exception:
            logger.exception("Failed to mark submitted meme button")
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
    sub = submission_queue.claim_pending(sub_id)
    if not sub:
        await callback.answer("уже обработано", show_alert=True)
        return

    if not POST_CHANNEL_ID:
        submission_queue.set_status(sub_id, "pending")
        await callback.answer("канал не настроен (POST_CHANNEL_ID/CHANNEL_ID)", show_alert=True)
        return

    try:
        if sub["text"]:
            await bot.send_photo(POST_CHANNEL_ID, sub["photo_file_id"], caption=sub["text"])
        else:
            await bot.send_photo(POST_CHANNEL_ID, sub["photo_file_id"])
    except Exception:
        submission_queue.set_status(sub_id, "pending")
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
    if not sub_id:
        await reset_state(state)
        return

    sub = submission_queue.get_submission(sub_id)
    if not sub or sub.get("status") != "pending":
        await reset_state(state)
        await message.answer("заявка уже обработана")
        return

    new_text = message.text.strip()
    if new_text == "-":
        new_text = ""
    if len(new_text) > MAX_SUBMISSION_TEXT:
        await message.answer(
            f"подпись слишком длинная: максимум {MAX_SUBMISSION_TEXT} символов, "
            f"сейчас {len(new_text)}. сократи и пришли ещё раз"
        )
        return
    await reset_state(state)
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
