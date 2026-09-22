"""Стикерпаки: складываем сгенерированные мемы и скрины в личные паки юзеров.

Три требования телеграма, из которых растёт вся конструкция:

1. У пака два разных имени. `name` — то, что в ссылке t.me/addstickers/...:
   только латиница, цифры и подчёркивания, начинается с буквы, без двойных
   подчёркиваний и обязано заканчиваться на "_by_<юзернейм бота>". `title` —
   человеческое название, 1-64 любых символа. Юзер вводит title, name мы
   генерим транслитом сами.
2. Стикер обязан быть WEBP или PNG со стороной 512 и весом до 512 КБ, так что
   file_id готового мема не переслать — картинку скачиваем и пережимаем.
3. Каждому стикеру нужен хотя бы один эмодзи.

Плюс потолок: 120 стикеров на пак, дальше нужен новый.
"""

from __future__ import annotations

import json
import random
import re
import string
from io import BytesIO
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    Message,
)
from PIL import Image

import storage

router = Router(name="stickers")

BTN_PACKS = "🧩 Мои стикерпаки"
BTN_TO_STICKERS = "🧩 В стикеры"
DEFAULT_EMOJI = "🗿"
# что предлагаем в один тап; «свой» уводит в ввод любого эмодзи
EMOJI_CHOICES = ("😁", "😭", "💀", "🔥", "❤️", "👍", "🤡", "🗿")
PACK_LIMIT = 120           # потолок телеграма для обычных паков
STICKER_SIDE = 512
STICKER_MAX_BYTES = 512 * 1024

PACKS_FILE = "sticker_packs.json"


class StickerStates(StatesGroup):
    waiting_title = State()     # ждём название нового пака
    waiting_photos = State()    # юзер льёт свои картинки пачкой
    waiting_emoji = State()     # ждём свой эмодзи для последнего стикера


# ---------- хранилище ----------

def _load() -> dict:
    path = storage.data_path(PACKS_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    storage.data_path(PACKS_FILE).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def user_packs(user_id: int) -> list:
    return _load().get(str(user_id), [])


def remember_pack(user_id: int, name: str, title: str) -> None:
    data = _load()
    packs = data.setdefault(str(user_id), [])
    if not any(p["name"] == name for p in packs):
        packs.append({"name": name, "title": title, "count": 1})
    _save(data)


def bump_pack(user_id: int, name: str) -> int:
    data = _load()
    for p in data.get(str(user_id), []):
        if p["name"] == name:
            p["count"] = p.get("count", 0) + 1
            _save(data)
            return p["count"]
    return 0


def forget_pack(user_id: int, name: str) -> None:
    """Пак удалён или недоступен — выкидываем из списка, чтобы не мозолил глаза."""
    data = _load()
    packs = data.get(str(user_id), [])
    data[str(user_id)] = [p for p in packs if p["name"] != name]
    _save(data)


# ---------- имя пака ----------

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def short_name(title: str, bot_username: str) -> str:
    """Имя для ссылки: транслит названия плюс случайный хвост от коллизий."""
    base = "".join(_TRANSLIT.get(c, c) for c in title.lower())
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    base = re.sub(r"_{2,}", "_", base)[:24].strip("_")
    if not base or not base[0].isalpha():
        base = "memes" + ("_" + base if base else "")
    tail = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(5))
    return f"{base}_{tail}_by_{bot_username}"


# ---------- картинка ----------

async def to_sticker_bytes(bot: Bot, file_id: str) -> Optional[bytes]:
    """Мем -> стикер: сторона 512, webp, влезть в 512 КБ."""
    try:
        buf = BytesIO()
        await bot.download(file_id, destination=buf)
        buf.seek(0)
        im = Image.open(buf).convert("RGBA")
    except Exception:
        return None
    w, h = im.size
    k = STICKER_SIDE / max(w, h)
    im = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
    for quality in (92, 80, 68, 55, 40):
        out = BytesIO()
        im.save(out, format="WEBP", quality=quality, method=4)
        if out.tell() <= STICKER_MAX_BYTES:
            return out.getvalue()
    return out.getvalue()


# ---------- работа с телеграмом ----------

_BOT_USERNAME: Optional[str] = None


async def bot_username(bot: Bot) -> str:
    global _BOT_USERNAME
    if _BOT_USERNAME is None:
        _BOT_USERNAME = (await bot.me()).username or "bot"
    return _BOT_USERNAME


def pack_link(name: str) -> str:
    return f"https://t.me/addstickers/{name}"


async def create_pack(bot: Bot, user_id: int, title: str, file_id: str) -> tuple:
    """Создаёт пак с первым стикером. Возвращает (имя пака, ошибка)."""
    data = await to_sticker_bytes(bot, file_id)
    if data is None:
        return None, "не смог забрать картинку, попробуй ещё раз"
    username = await bot_username(bot)
    sticker = InputSticker(
        sticker=BufferedInputFile(data, filename="sticker.webp"),
        format="static", emoji_list=[DEFAULT_EMOJI])
    # имя генерится случайно, но занятые имена бывают — пробуем несколько раз
    for _ in range(5):
        name = short_name(title, username)
        try:
            await bot.create_new_sticker_set(
                user_id=user_id, name=name, title=title[:64], stickers=[sticker])
            remember_pack(user_id, name, title[:64])
            return name, None
        except Exception as e:
            text = str(e).lower()
            if "occupied" in text or "invalid" in text and "name" in text:
                continue
            if "peer_id_invalid" in text or "user not found" in text:
                return None, "телеграм не даёт создать пак для этого аккаунта"
            return None, f"телеграм отказал: {e}"
    return None, "не вышло подобрать свободное имя, попробуй другое название"


async def add_to_pack(bot: Bot, user_id: int, name: str, file_id: str) -> Optional[str]:
    """Докладывает стикер в пак. Возвращает текст ошибки или None."""
    data = await to_sticker_bytes(bot, file_id)
    if data is None:
        return "не смог забрать картинку, попробуй ещё раз"
    sticker = InputSticker(
        sticker=BufferedInputFile(data, filename="sticker.webp"),
        format="static", emoji_list=[DEFAULT_EMOJI])
    try:
        await bot.add_sticker_to_set(user_id=user_id, name=name, sticker=sticker)
        bump_pack(user_id, name)
        return None
    except Exception as e:
        text = str(e).lower()
        if "stickers_too_much" in text or "too much" in text:
            return "пак заполнен под завязку, заведи новый"
        if "invalid" in text and "sticker" in text and "set" in text:
            forget_pack(user_id, name)
            return "этот пак больше не существует, я убрал его из списка"
        return f"телеграм отказал: {e}"


# ---------- клавиатуры ----------

def sticker_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=BTN_TO_STICKERS, callback_data="stk:add")


def choose_pack_kb(packs: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{p['title']} · {p.get('count', 0)}/{PACK_LIMIT}",
                                  callback_data=f"stk:to:{i}")]
            for i, p in enumerate(packs[:8])]
    rows.append([InlineKeyboardButton(text="➕ новый пак", callback_data="stk:new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def packs_kb(packs: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ создать пак", callback_data="stk:new")]]
    if packs:
        rows.insert(0, [InlineKeyboardButton(text="📥 накидать картинок",
                                             callback_data="stk:dump")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- сценарии ----------

def _photo_id(message: Message) -> Optional[str]:
    return message.photo[-1].file_id if message.photo else None


@router.callback_query(F.data == "stk:add")
async def to_stickers(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    file_id = _photo_id(callback.message)
    if not file_id:
        await callback.answer("картинка потерялась", show_alert=True)
        return
    packs = user_packs(callback.from_user.id)
    if not packs:
        await state.set_state(StickerStates.waiting_title)
        await state.update_data(stk_file=file_id)
        await callback.answer()
        await callback.message.answer(
            "стикерпака у тебя ещё нет, давай заведём\n\n"
            "как его назвать? это имя увидят все, кому пришлёшь стикер")
        return
    if len(packs) == 1:
        await callback.answer("кладу")
        err = await add_to_pack(bot, callback.from_user.id, packs[0]["name"], file_id)
        await _report(callback.message, callback.from_user.id, packs[0], err, bot, state)
        return
    await callback.answer()
    await state.update_data(stk_file=file_id)
    await callback.message.answer("в какой пак?", reply_markup=choose_pack_kb(packs))


async def last_sticker_id(bot: Bot, name: str) -> Optional[str]:
    """file_id только что добавленного стикера — он нужен, чтобы сменить эмодзи.
    add_sticker_to_set возвращает лишь True, поэтому перечитываем пак."""
    try:
        st = await bot.get_sticker_set(name)
        return st.stickers[-1].file_id if st.stickers else None
    except Exception:
        return None


def emoji_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=e, callback_data=f"stk:emo:{e}")
             for e in EMOJI_CHOICES[i:i + 4]] for i in (0, 4)]
    rows.append([InlineKeyboardButton(text="свой эмодзи", callback_data="stk:emo:custom")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _report(message: Message, user_id: int, pack: dict, err: Optional[str],
                  bot: Optional[Bot] = None, state: Optional[FSMContext] = None) -> None:
    if err:
        await message.answer(err)
        return
    count = next((p.get("count", 0) for p in user_packs(user_id)
                  if p["name"] == pack["name"]), pack.get("count", 0))
    kb = None
    if bot is not None and state is not None:
        sid = await last_sticker_id(bot, pack["name"])
        if sid:
            await state.update_data(stk_last=sid)
            kb = emoji_kb()
    await message.answer(
        f"готово, стикер в паке «{pack['title']}» ({count}/{PACK_LIMIT}), эмодзи {DEFAULT_EMOJI}\n"
        f"{pack_link(pack['name'])}",
        reply_markup=kb)


@router.callback_query(F.data.startswith("stk:to:"))
async def pick_pack(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    packs = user_packs(callback.from_user.id)
    idx = int(callback.data.split(":")[2])
    if idx >= len(packs):
        await callback.answer("пак пропал", show_alert=True)
        return
    data = await state.get_data()
    file_id = data.get("stk_file")
    if not file_id:
        await callback.answer("картинка потерялась, нажми «в стикеры» заново", show_alert=True)
        return
    await callback.answer("кладу")
    err = await add_to_pack(bot, callback.from_user.id, packs[idx]["name"], file_id)
    await _report(callback.message, callback.from_user.id, packs[idx], err, bot, state)


@router.callback_query(F.data == "stk:new")
async def new_pack(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(StickerStates.waiting_title)
    await callback.answer()
    await callback.message.answer("как назвать новый пак?")


@router.message(StickerStates.waiting_title, F.text)
async def got_title(message: Message, state: FSMContext, bot: Bot) -> None:
    title = message.text.strip()[:64]
    data = await state.get_data()
    file_id = data.get("stk_file")
    await state.set_state(None)
    if not file_id:
        await state.update_data(stk_pending_title=title)
        await state.set_state(StickerStates.waiting_photos)
        await message.answer(f"пак «{title}» заведу на первой же картинке — пришли её")
        return
    name, err = await create_pack(bot, message.from_user.id, title, file_id)
    if err:
        await message.answer(err)
        return
    kb = None
    sid = await last_sticker_id(bot, name)
    if sid:
        await state.update_data(stk_last=sid)
        kb = emoji_kb()
    await message.answer(
        f"пак «{title}» готов, стикер внутри с эмодзи {DEFAULT_EMOJI}\n{pack_link(name)}\n\n"
        f"дальше кнопка «{BTN_TO_STICKERS}» под мемами будет класть сюда",
        reply_markup=kb)


@router.callback_query(F.data == "stk:dump")
async def dump_mode(callback: CallbackQuery, state: FSMContext) -> None:
    packs = user_packs(callback.from_user.id)
    if not packs:
        await callback.answer("сначала заведи пак", show_alert=True)
        return
    await state.set_state(StickerStates.waiting_photos)
    await state.update_data(stk_pack=packs[0]["name"])
    await callback.answer()
    await callback.message.answer(
        f"кидай картинки — каждая уйдёт в «{packs[0]['title']}»\n"
        "когда надоест, жми отмену")


@router.message(StickerStates.waiting_photos, F.photo)
async def dump_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    file_id = message.photo[-1].file_id
    title = data.get("stk_pending_title")
    if title:                                   # первая картинка создаёт пак
        name, err = await create_pack(bot, message.from_user.id, title, file_id)
        if err:
            await message.answer(err)
            return
        sid = await last_sticker_id(bot, name)
        await state.update_data(stk_pending_title=None, stk_pack=name, stk_last=sid)
        await message.answer(f"пак «{title}» создан, эмодзи {DEFAULT_EMOJI}\n{pack_link(name)}",
                             reply_markup=emoji_kb() if sid else None)
        return
    name = data.get("stk_pack")
    packs = user_packs(message.from_user.id)
    pack = next((p for p in packs if p["name"] == name), packs[0] if packs else None)
    if pack is None:
        await message.answer("пак потерялся, заведи новый")
        return
    err = await add_to_pack(bot, message.from_user.id, pack["name"], file_id)
    await _report(message, message.from_user.id, pack, err, bot, state)


@router.message(F.text == BTN_PACKS)
async def my_packs(message: Message) -> None:
    packs = user_packs(message.from_user.id)
    if not packs:
        await message.answer(
            "паков пока нет\n\n"
            f"проще всего завести так: сделай мем и нажми под ним «{BTN_TO_STICKERS}»",
            reply_markup=packs_kb(packs))
        return
    lines = ["твои паки:", ""]
    for p in packs:
        lines.append(f"«{p['title']}» — {p.get('count', 0)}/{PACK_LIMIT}\n{pack_link(p['name'])}")
    await message.answer("\n".join(lines), reply_markup=packs_kb(packs))


# ---------- эмодзи у стикера ----------

async def _apply_emoji(bot: Bot, state: FSMContext, emoji: str) -> Optional[str]:
    data = await state.get_data()
    sid = data.get("stk_last")
    if not sid:
        return "не помню, какому стикеру менять — добавь новый и жми сразу"
    try:
        await bot.set_sticker_emoji_list(sticker=sid, emoji_list=[emoji])
        return None
    except Exception as e:
        return f"телеграм отказал: {e}"


@router.callback_query(F.data.startswith("stk:emo:"))
async def change_emoji(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    value = callback.data.split("stk:emo:", 1)[1]
    if value == "custom":
        await state.update_data(stk_return_state=await state.get_state())
        await state.set_state(StickerStates.waiting_emoji)
        await callback.answer()
        await callback.message.answer("пришли эмодзи, который повесить на стикер")
        return
    err = await _apply_emoji(bot, state, value)
    await callback.answer(err or f"теперь {value}", show_alert=bool(err))
    if not err:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@router.message(StickerStates.waiting_emoji, F.text)
async def custom_emoji(message: Message, state: FSMContext, bot: Bot) -> None:
    import chatgen
    found = [r[1] for r in chatgen.split_runs(message.text.strip()) if r[0] == "emoji"]
    if not found:
        await message.answer("это не эмодзи, пришли один смайлик")
        return
    data = await state.get_data()
    await state.set_state(data.get("stk_return_state"))   # вернуться туда, где был
    err = await _apply_emoji(bot, state, found[0])
    await message.answer(err or f"поставил {found[0]}")
