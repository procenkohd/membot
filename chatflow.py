"""Сценарий «сделать переписку»: бот по шагам собирает фейковый скрин чата.

Главный принцип интерфейса — не заставлять человека вводить служебные символы
и не спрашивать лишнего. В режиме конструктора обычный текст сразу становится
репликой, присланное фото — картинкой в чате, а кнопки нужны только для того,
что текстом не выразишь: голосовое, кружок, звонок, реакция.

Черновик лежит в FSM (в памяти), картинки хранятся как file_id и скачиваются
только в момент отрисовки — иначе на каждое сообщение висел бы мегабайт.
"""

from __future__ import annotations

import asyncio
import html
import random
from io import BytesIO
from pathlib import Path
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InputMediaPhoto,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from PIL import Image

import chatgen
import stickers
import ui

router = Router(name="chatflow")

BTN_CHAT = "💬 Создать фейк-переписку"
SKIP = "chat:skip"

cancel_kb = ui.cancel_kb    # выход обрабатывает общий хендлер в bot.py


class ChatStates(StatesGroup):
    contact_name = State()      # имя собеседника (оно же в шапке)
    contact_avatar = State()    # его аватарка
    my_name = State()           # как зовут самого пользователя
    my_avatar = State()
    settings = State()          # тема, время, шаг — всё кнопками
    builder = State()           # основной цикл: пишем реплики
    wait_photo_for = State()    # ждём фото для кружка
    wait_sticker = State()      # ждём эмодзи для стикера
    wait_file_name = State()    # ждём название файла
    member_name = State()       # ждём имя очередного участника группы
    member_avatar = State()     # ждём его аватарку


# ---------- черновик ----------

def blank_draft() -> dict:
    return {
        "contact_name": "", "contact_avatar": None,
        "my_name": "", "my_avatar": None,
        "theme": "ios_teal", "start": "12:00", "step": 60,
        # группа: участники кроме тебя и кто сейчас говорит (-1 — ты)
        "kind": "duo",
        "members": [],
        "speaker": -1,
        "speaker_out": False,      # False — пишет собеседник; только для duo
        "reply_next": False,       # следующая реплика будет ответом на предыдущую
        "items": [],
    }


def is_group(draft: dict) -> bool:
    return draft.get("kind") == "group"


def sender_name(draft: dict, idx: int) -> str:
    """-1 — сам пользователь, остальное — индекс в списке участников."""
    if idx < 0:
        return draft["my_name"]
    members = draft.get("members") or []
    return members[idx]["name"] if idx < len(members) else "?"


def item_sender(draft: dict, item: dict) -> int:
    """Кто автор реплики. В переписке на двоих индекса нет, там решает out."""
    if "sender" in item:
        return item["sender"]
    return -1 if item.get("out") else 0


def current_speaker(draft: dict) -> str:
    if is_group(draft):
        return sender_name(draft, draft.get("speaker", -1))
    return draft["my_name"] if draft["speaker_out"] else draft["contact_name"]


def _hhmm(minutes: int) -> str:
    return f"{(minutes // 60) % 24}:{minutes % 60:02d}"


def _start_minutes(draft: dict) -> int:
    try:
        h, m = draft["start"].split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 12 * 60


def item_times(draft: dict) -> list:
    """Время у каждой реплики: старт плюс шаг. «Случайный» шаг (step=0) даёт
    неровные паузы — так переписка выглядит живее ровной сетки."""
    rnd = random.Random(len(draft["items"]) * 7 + _start_minutes(draft))
    minutes = _start_minutes(draft)
    out, acc_seconds = [], 0
    for i, _ in enumerate(draft["items"]):
        if i:
            step = draft["step"] or rnd.choice((30, 60, 60, 120, 180, 420))
            acc_seconds += step
        out.append(_hhmm(minutes + acc_seconds // 60))
    return out


VISIBLE_ITEMS = 15      # длиннее список не влезет в лимит сообщения телеграма


def describe(draft: dict) -> str:
    """Панель конструктора. Размечена HTML: главное — кто сейчас говорит и как
    это переключить, поэтому оно жирным. Имена и реплики экранируем, иначе
    угловая скобка в тексте сломает разметку."""
    e = html.escape
    who = current_speaker(draft)
    if is_group(draft):
        head = ", ".join([draft["my_name"]] + [m["name"] for m in draft.get("members", [])])
        lines = [f"<b>Группа «{e(draft['contact_name'])}»:</b> {e(head)}"]
    else:
        lines = [f"<b>Переписка:</b> {e(draft['contact_name'])} и {e(draft['my_name'])}"]

    if not draft["items"]:
        lines += ["", "пока пусто"]
    else:
        pages = chatgen.page_count(_preview_msgs(draft), theme=draft["theme"])
        lines.append(f"реплик: {len(draft['items'])} · выйдет "
                     f"{pages} {'скрин' if pages == 1 else 'скрина' if pages < 5 else 'скринов'}")
        lines.append("")
        times = item_times(draft)
        shown = list(enumerate(zip(draft["items"], times), 1))
        if len(shown) > VISIBLE_ITEMS:
            lines.append(f"…и ещё {len(shown) - VISIBLE_ITEMS} выше")
            shown = shown[-VISIBLE_ITEMS:]
        for i, (it, t) in shown:
            name = sender_name(draft, item_sender(draft, it))
            body = {
                "text": it.get("text", ""),
                "photo": "🖼 фото" + (f": {it['text']}" if it.get("text") else ""),
                "voice": f"🎤 голосовое {it.get('duration', '')}",
                "videonote": f"⭕ кружок {it.get('duration', '')}",
                "sticker": f"стикер {it.get('sticker', '')}",
                "file": f"📎 файл: {it.get('file_name', '')}",
                "call": "📞 " + ("пропущенный звонок" if it.get("call_missed")
                                 else "входящий звонок"),
            }.get(it["kind"], it["kind"])
            mark = "↩️ " if it.get("reply_to") is not None else ""
            react = f" {it['reaction']}" if it.get("reaction") else ""
            lines.append(f"{i}. {mark}<b>{e(name)}:</b> {e(str(body))}{react}  ·{t}")

    lines += [
        "",
        f"<b>Сейчас пишет: {e(who)}</b>",
        "Пиши текст или кидай фото — добавится сразу.",
        # имена не склоняем и род не угадываем: подставленное имя в падеже
        # звучало бы коряво почти всегда
        ("<b>Чтобы говорил другой — ткни в его имя кнопкой выше</b>" if is_group(draft)
         else "<b>Чтобы говорил другой — жми «🔄 сейчас пишет»</b>"),
    ]
    return "\n".join(lines)


# одна заглушка на всех: реальные пропорции для счётчика не важны, а плодить
# по картинке на каждое фото — лишние мегабайты
_PLACEHOLDER = Image.new("RGB", (1000, 750))


def _preview_msgs(draft: dict) -> list:
    """Сообщения без картинок — только чтобы посчитать, на сколько экранов
    разъедется переписка. Скачивать ради счётчика фото было бы расточительно."""
    out = []
    for it, t in zip(draft["items"], item_times(draft)):
        out.append(chatgen.Msg(
            text=it.get("text", ""), out=it["out"], time=t, kind=it["kind"],
            duration=it.get("duration", ""), file_name=it.get("file_name", ""),
            sticker=it.get("sticker", ""), reaction=it.get("reaction"),
            sender=(sender_name(draft, item_sender(draft, it))
                    if (is_group(draft) and item_sender(draft, it) >= 0) else None),
            reply_name="x" if it.get("reply_to") is not None else None,
            reply_text="x" if it.get("reply_to") is not None else None,
            date=it.get("date"),
            photo=_PLACEHOLDER if it.get("photo") else None,
        ))
    return out


def builder_kb(draft: dict) -> InlineKeyboardMarkup:
    reply_mark = "✅" if draft["reply_next"] else "↩️"
    if is_group(draft):
        # в группе вместо переключателя — список участников, текущий с точкой
        people = [(-1, draft["my_name"])]
        people += [(i, m["name"]) for i, m in enumerate(draft.get("members", []))]
        cur = draft.get("speaker", -1)
        rows = [[InlineKeyboardButton(
            text=("● " if i == cur else "") + name[:16], callback_data=f"chat:who:{i}")
            for i, name in people[k:k + 3]] for k in range(0, len(people), 3)]
    else:
        rows = [[InlineKeyboardButton(
            text=f"🔄 сейчас пишет: {current_speaker(draft)}", callback_data="chat:swap")]]
    rows += [
        [InlineKeyboardButton(text="🎤 голосовое", callback_data="chat:add:voice"),
         InlineKeyboardButton(text="⭕ кружок", callback_data="chat:add:videonote")],
        [InlineKeyboardButton(text="😀 стикер", callback_data="chat:add:sticker"),
         InlineKeyboardButton(text="📎 файл", callback_data="chat:add:file"),
         InlineKeyboardButton(text="📞 звонок", callback_data="chat:add:call")],
        [InlineKeyboardButton(text="❤️ реакция", callback_data="chat:react"),
         InlineKeyboardButton(text=f"{reply_mark} ответом", callback_data="chat:reply")],
        [InlineKeyboardButton(text="📅 дата", callback_data="chat:add:date"),
         InlineKeyboardButton(text="🗑 убрать последнее", callback_data="chat:undo")],
        [InlineKeyboardButton(text="✅ готово, рисуй", callback_data="chat:render")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_builder(message: Message, draft: dict, edit: bool = False,
                       state: Optional[FSMContext] = None) -> None:
    """Панель конструктора всегда должна быть последним сообщением в чате,
    иначе после каждой реплики она уезжает вверх, а кнопки под ней остаются
    живыми. Поэтому при обновлении старую панель удаляем, а не плодим новые."""
    text, kb = describe(draft), builder_kb(draft)
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    if state is not None:
        data = await state.get_data()
        old_id = data.get("builder_msg")
        if old_id:
            try:
                await message.bot.delete_message(message.chat.id, old_id)
            except Exception:
                pass
    sent = await message.answer(text, reply_markup=kb, parse_mode="HTML")
    if state is not None:
        await state.update_data(builder_msg=sent.message_id)


# ---------- сборка картинки ----------

async def _img(bot: Bot, file_id: Optional[str]) -> Optional[Image.Image]:
    if not file_id:
        return None
    try:
        buf = BytesIO()
        await bot.download(file_id, destination=buf)
        buf.seek(0)
        return Image.open(buf).convert("RGB")
    except Exception:
        return None


async def build_messages(bot: Bot, draft: dict) -> list:
    times = item_times(draft)
    msgs, cache = [], {}
    for i, (it, t) in enumerate(zip(draft["items"], times)):
        photo = None
        fid = it.get("photo")
        if fid:
            if fid not in cache:
                cache[fid] = await _img(bot, fid)
            photo = cache[fid]
        reply_name = reply_text = None
        if it.get("reply_to") is not None:
            src = draft["items"][it["reply_to"]]
            reply_name = sender_name(draft, item_sender(draft, src))
            reply_text = src.get("text") or REPLY_LABELS.get(src["kind"], "Сообщение")
        # аватарка автора реплики — в группе она рисуется слева от пузыря
        sender_idx = item_sender(draft, it)
        sender_ava = None
        if is_group(draft) and sender_idx >= 0:
            members = draft.get("members", [])
            fid = members[sender_idx].get("avatar") if sender_idx < len(members) else None
            if fid:
                if fid not in cache:
                    cache[fid] = await _img(bot, fid)
                sender_ava = cache[fid]

        # реакцию ставит «другая сторона», поэтому и аватарка её
        reactor_photo = None
        if it.get("reaction"):
            fid = draft.get("contact_avatar") if it["out"] else draft.get("my_avatar")
            if fid:
                if fid not in cache:
                    cache[fid] = await _img(bot, fid)
                reactor_photo = cache[fid]
        msgs.append(chatgen.Msg(
            text=it.get("text", ""), out=it["out"], time=t,
            kind=it["kind"] if it["kind"] != "date" else "text",
            photo=photo, duration=it.get("duration", ""),
            file_name=it.get("file_name", ""), file_size=it.get("file_size", ""),
            call_missed=bool(it.get("call_missed")), sticker=it.get("sticker", ""),
            reaction=it.get("reaction"), reactor_photo=reactor_photo,
            sender=(sender_name(draft, sender_idx)
                    if (is_group(draft) and sender_idx >= 0) else None),
            sender_color=sender_idx if sender_idx >= 0 else 0,
            avatar=sender_ava,
            reply_name=reply_name, reply_text=reply_text,
            date=it.get("date"),
        ))
    return msgs


MAX_MEMBERS = 6     # больше в палитре имён всё равно нет цветов
MAX_PAGES = 10          # ровно столько картинок влезает в один альбом телеграма


async def render_draft(bot: Bot, draft: dict) -> list:
    """Готовые скрины. Длинная переписка сама разъезжается на несколько — так
    же, как её скриншотил бы человек, листая чат."""
    msgs = await build_messages(bot, draft)
    avatar = await _img(bot, draft.get("contact_avatar"))
    # рисование синхронное и на слабом ядре занимает заметное время — уводим в
    # поток, иначе на время отрисовки бот замирает для всех остальных
    group = is_group(draft)
    subtitle = (f"{len(draft.get('members', [])) + 1} участника, 2 в сети"
                if group else "был(а) недавно")
    return await asyncio.to_thread(
        chatgen.make_chat_pages, msgs, max_pages=MAX_PAGES, group=group,
        theme=draft["theme"], contact_name=draft["contact_name"] or "Контакт",
        subtitle=subtitle, avatar=avatar,
        # без сида: иначе при одинаковом числе реплик счётчик повторяется
        unread=random.choice((None, random.randint(1, 12), random.randint(13, 400),
                              random.randint(400, 9999))),
        clock=draft["start"],
    )


# ---------- мастер: собираем участников ----------

def skip_kb(text: str = "пропустить") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=SKIP)]])


def mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 на двоих", callback_data="chat:mode:duo")],
        [InlineKeyboardButton(text="👥 групповой чат", callback_data="chat:mode:group")],
    ])


@router.message(F.text == BTN_CHAT)
async def chat_start(message: Message, state: FSMContext) -> None:
    await state.set_state(ChatStates.settings)
    await state.update_data(draft=blank_draft())
    await message.answer("делаем переписку. я проведу по шагам", reply_markup=cancel_kb)
    await message.answer("какой чат рисуем?", reply_markup=mode_kb())


@router.callback_query(F.data.startswith("chat:mode:"))
async def pick_mode(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("draft") or blank_draft()
    draft["kind"] = callback.data.split(":")[2]
    await state.update_data(draft=draft)
    await state.set_state(ChatStates.contact_name)
    await callback.answer()
    ask = ("как называется группа? это увидишь в шапке чата"
           if is_group(draft) else
           "как зовут собеседника? это имя будет в шапке чата")
    await callback.message.edit_text(ask)


@router.message(ChatStates.contact_name, F.text)
async def got_contact_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    draft["contact_name"] = message.text.strip()[:40]
    await state.update_data(draft=draft)
    await state.set_state(ChatStates.contact_avatar)
    what = "группы" if is_group(draft) else ""
    await message.answer(f"есть. пришли фото для аватарки {what} «{draft['contact_name']}»",
                         reply_markup=skip_kb("без аватарки"))


@router.message(ChatStates.contact_avatar, F.photo)
async def got_contact_avatar(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    draft["contact_avatar"] = message.photo[-1].file_id
    await state.update_data(draft=draft)
    await _ask_my_name(message, state)


async def _ask_my_name(message: Message, state: FSMContext) -> None:
    await state.set_state(ChatStates.my_name)
    await message.answer("а как подписать тебя? твои сообщения будут справа\n"
                         "имя видно, когда на сообщение отвечают")


@router.message(ChatStates.my_name, F.text)
async def got_my_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    draft["my_name"] = message.text.strip()[:40]
    await state.update_data(draft=draft)
    await state.set_state(ChatStates.my_avatar)
    await message.answer("пришли своё фото — оно появится рядом с реакциями",
                         reply_markup=skip_kb("без аватарки"))


@router.message(ChatStates.my_avatar, F.photo)
async def got_my_avatar(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    draft["my_avatar"] = message.photo[-1].file_id
    await state.update_data(draft=draft)
    await _after_me(message, state, draft)


async def _after_me(message: Message, state: FSMContext, draft: dict) -> None:
    if is_group(draft):
        await _ask_member(message, state, draft)
    else:
        await _ask_theme(message, state)


async def _ask_member(message: Message, state: FSMContext, draft: dict) -> None:
    await state.set_state(ChatStates.member_name)
    n = len(draft.get("members", [])) + 1
    await message.answer(f"как зовут участника №{n}?")


@router.message(ChatStates.member_name, F.text)
async def got_member_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    draft.setdefault("members", []).append(
        {"name": message.text.strip()[:40], "avatar": None})
    await state.update_data(draft=draft)
    await state.set_state(ChatStates.member_avatar)
    await message.answer(f"аватарка для «{draft['members'][-1]['name']}»?",
                         reply_markup=skip_kb("без аватарки"))


@router.message(ChatStates.member_avatar, F.photo)
async def got_member_avatar(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    draft["members"][-1]["avatar"] = message.photo[-1].file_id
    await state.update_data(draft=draft)
    await _ask_more_members(message, state, draft)


async def _ask_more_members(message: Message, state: FSMContext, draft: dict) -> None:
    names = ", ".join(m["name"] for m in draft["members"])
    if len(draft["members"]) >= MAX_MEMBERS:
        await message.answer(f"участники: {names}. больше не влезет, идём дальше")
        await _ask_theme(message, state)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ ещё участник", callback_data="chat:member:more"),
        InlineKeyboardButton(text="хватит", callback_data="chat:member:done"),
    ]])
    await message.answer(f"в группе: {draft['my_name']}, {names}", reply_markup=kb)


@router.callback_query(F.data.startswith("chat:member:"))
async def more_members(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    if draft is None:
        await callback.answer("переписка потерялась, начни заново", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if callback.data.endswith("more"):
        await _ask_member(callback.message, state, draft)
    else:
        await _ask_theme(callback.message, state)


THEME_PREVIEW = Path(__file__).parent / "assets" / "theme_preview.jpg"


async def _ask_theme(message: Message, state: FSMContext) -> None:
    """Оформление показываем картинкой: словами «тема» человек понимает
    «о чём переписка», а не «как она выглядит»."""
    await state.set_state(ChatStates.settings)
    # порядок кнопок обязан совпадать с порядком на превью: там номера
    keys = list(chatgen.THEMES)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(i + 1), callback_data=f"chat:theme:{k}")
         for i, k in enumerate(keys)][row:row + 6]
        for row in (0, 6)
    ])
    caption = "как должен выглядеть чат? жми номер с картинки"
    if THEME_PREVIEW.exists():
        await message.answer_photo(FSInputFile(THEME_PREVIEW), caption=caption, reply_markup=kb)
    else:
        await message.answer(caption, reply_markup=kb)


@router.callback_query(F.data == SKIP)
async def skip_step(callback: CallbackQuery, state: FSMContext) -> None:
    cur = await state.get_state()
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if cur == ChatStates.contact_avatar.state:
        await _ask_my_name(callback.message, state)
    elif cur == ChatStates.my_avatar.state:
        data = await state.get_data()
        await _after_me(callback.message, state, data["draft"])
    elif cur == ChatStates.member_avatar.state:
        data = await state.get_data()
        await _ask_more_members(callback.message, state, data["draft"])
    elif cur == ChatStates.wait_photo_for.state:
        await _finish_videonote(callback.message, state, None)


@router.callback_query(F.data.startswith("chat:theme:"))
async def pick_theme(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    if draft is None:
        await callback.answer("переписка потерялась, начни заново", show_alert=True)
        return
    draft["theme"] = callback.data.split(":")[2]
    await state.update_data(draft=draft)
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="утро, 09:41", callback_data="chat:time:09:41"),
         InlineKeyboardButton(text="день, 14:20", callback_data="chat:time:14:20")],
        [InlineKeyboardButton(text="вечер, 21:07", callback_data="chat:time:21:07"),
         InlineKeyboardButton(text="ночь, 03:12", callback_data="chat:time:03:12")],
    ])
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("во сколько идёт переписка?", reply_markup=kb)


@router.callback_query(F.data.startswith("chat:time:"))
async def pick_time(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    if draft is None:
        await callback.answer("переписка потерялась, начни заново", show_alert=True)
        return
    draft["start"] = callback.data.split("chat:time:")[1]
    await state.update_data(draft=draft)
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="полминуты", callback_data="chat:step:30"),
         InlineKeyboardButton(text="минута", callback_data="chat:step:60")],
        [InlineKeyboardButton(text="две", callback_data="chat:step:120"),
         InlineKeyboardButton(text="пять", callback_data="chat:step:300")],
        [InlineKeyboardButton(text="вразнобой, как в жизни", callback_data="chat:step:0")],
    ])
    await callback.message.edit_text("сколько проходит между сообщениями?", reply_markup=kb)


@router.callback_query(F.data.startswith("chat:step:"))
async def pick_step(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    if draft is None:
        await callback.answer("переписка потерялась, начни заново", show_alert=True)
        return
    draft["step"] = int(callback.data.split(":")[2])
    await state.update_data(draft=draft)
    await state.set_state(ChatStates.builder)
    await callback.answer()
    await callback.message.edit_text(
        "всё, настроили. дальше просто пиши реплики — каждая станет сообщением.\n"
        "кидай фото — станет картинкой в чате. эмодзи работают как обычно")
    await show_builder(callback.message, draft, state=state)


# ---------- конструктор ----------

REPLY_LABELS = {
    "photo": "Фото", "voice": "Голосовое сообщение", "videonote": "Видеосообщение",
    "sticker": "Стикер", "file": "Файл", "call": "Звонок",
}
FILE_SIZES = ("1,2 МБ", "2,4 МБ", "640 КБ", "8,1 МБ", "14,7 МБ", "312 КБ")
POPULAR = ("😁", "😭", "💀", "🔥", "❤️", "👍", "🤡", "🗿")


def _add(draft: dict, **fields) -> None:
    """Добавляет элемент от текущего говорящего и гасит разовые флаги."""
    if is_group(draft):
        who = draft.get("speaker", -1)
        item = {"kind": "text", "out": who < 0, "sender": who, **fields}
    else:
        item = {"kind": "text", "out": draft["speaker_out"], **fields}
    if draft.get("reply_next") and draft["items"]:
        item["reply_to"] = len(draft["items"]) - 1
        draft["reply_next"] = False
    if draft.get("pending_date"):
        item["date"] = draft.pop("pending_date")
    draft["items"].append(item)


async def _save_and_refresh(message: Message, state: FSMContext, draft: dict) -> None:
    await state.update_data(draft=draft)
    await show_builder(message, draft, state=state)


@router.message(ChatStates.builder, F.text)
async def builder_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    _add(draft, kind="text", text=message.text.strip()[:400])
    await _save_and_refresh(message, state, draft)


@router.message(ChatStates.builder, F.photo)
async def builder_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    _add(draft, kind="photo", photo=message.photo[-1].file_id,
         text=(message.caption or "").strip()[:400])
    await _save_and_refresh(message, state, draft)


@router.callback_query(ChatStates.builder, F.data == "chat:swap")
async def swap_speaker(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    draft["speaker_out"] = not draft["speaker_out"]
    await state.update_data(draft=draft)
    await callback.answer()
    await show_builder(callback.message, draft, edit=True)


@router.callback_query(ChatStates.builder, F.data.startswith("chat:who:"))
async def pick_speaker(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    draft["speaker"] = int(callback.data.split(":")[2])
    await state.update_data(draft=draft)
    await callback.answer(f"пишет {current_speaker(draft)}")
    await show_builder(callback.message, draft, edit=True)


@router.callback_query(ChatStates.builder, F.data == "chat:reply")
async def toggle_reply(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    if not draft["items"]:
        await callback.answer("сначала напиши хоть одну реплику", show_alert=True)
        return
    draft["reply_next"] = not draft["reply_next"]
    await state.update_data(draft=draft)
    await callback.answer("следующая реплика будет ответом" if draft["reply_next"] else "отменил")
    await show_builder(callback.message, draft, edit=True)


@router.callback_query(ChatStates.builder, F.data == "chat:undo")
async def undo(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    if draft["items"]:
        draft["items"].pop()
    await state.update_data(draft=draft)
    await callback.answer("убрал")
    await show_builder(callback.message, draft, edit=True)


@router.callback_query(ChatStates.builder, F.data == "chat:react")
async def ask_reaction(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data["draft"]["items"]:
        await callback.answer("сначала напиши реплику", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=e, callback_data=f"chat:setreact:{e}")
             for e in POPULAR[i:i + 4]] for i in (0, 4)]
    rows.append([InlineKeyboardButton(text="← назад", callback_data="chat:back")])
    await callback.answer()
    await callback.message.edit_text("какую реакцию поставить на последнее сообщение?",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(ChatStates.builder, F.data.startswith("chat:setreact:"))
async def set_reaction(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    draft["items"][-1]["reaction"] = callback.data.split("chat:setreact:")[1]
    await state.update_data(draft=draft)
    await callback.answer()
    await show_builder(callback.message, draft, edit=True)


@router.callback_query(ChatStates.builder, F.data == "chat:back")
async def back_to_builder(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await callback.answer()
    await show_builder(callback.message, data["draft"], edit=True)


@router.callback_query(ChatStates.builder, F.data.startswith("chat:add:"))
async def add_element(callback: CallbackQuery, state: FSMContext) -> None:
    kind = callback.data.split(":")[2]
    data = await state.get_data()
    draft = data["draft"]
    await callback.answer()

    if kind == "voice":
        rows = [[InlineKeyboardButton(text=d, callback_data=f"chat:dur:voice:{d}")
                 for d in ("0:07", "0:25")],
                [InlineKeyboardButton(text=d, callback_data=f"chat:dur:voice:{d}")
                 for d in ("0:57", "2:13")],
                [InlineKeyboardButton(text="← назад", callback_data="chat:back")]]
        await callback.message.edit_text("какой длины голосовое?",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif kind == "videonote":
        await state.set_state(ChatStates.wait_photo_for)
        await callback.message.edit_text(
            "пришли фото — оно станет кадром кружка", reply_markup=skip_kb("без фото"))
    elif kind == "sticker":
        rows = [[InlineKeyboardButton(text=e, callback_data=f"chat:setsticker:{e}")
                 for e in POPULAR[i:i + 4]] for i in (0, 4)]
        rows.append([InlineKeyboardButton(text="← назад", callback_data="chat:back")])
        await state.set_state(ChatStates.wait_sticker)
        await callback.message.edit_text(
            "выбери стикер или пришли любой эмодзи сообщением",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif kind == "file":
        await state.set_state(ChatStates.wait_file_name)
        await callback.message.edit_text("как называется файл? например «договор.pdf»")
    elif kind == "call":
        rows = [[InlineKeyboardButton(text="входящий", callback_data="chat:call:in"),
                 InlineKeyboardButton(text="пропущенный", callback_data="chat:call:miss")],
                [InlineKeyboardButton(text="← назад", callback_data="chat:back")]]
        await callback.message.edit_text("какой звонок?",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif kind == "date":
        rows = [[InlineKeyboardButton(text=d, callback_data=f"chat:date:{d}")
                 for d in ("Сегодня", "Вчера")],
                [InlineKeyboardButton(text="← назад", callback_data="chat:back")]]
        await callback.message.edit_text("какую дату поставить перед следующим сообщением?",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(ChatStates.builder, F.data.startswith("chat:dur:"))
async def set_duration(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, kind, dur = callback.data.split(":", 3)
    data = await state.get_data()
    draft = data["draft"]
    _add(draft, kind=kind, duration=dur)
    await state.update_data(draft=draft)
    await callback.answer()
    await show_builder(callback.message, draft, edit=True)


@router.callback_query(ChatStates.builder, F.data.startswith("chat:call:"))
async def set_call(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    _add(draft, kind="call", call_missed=callback.data.endswith("miss"),
         duration="" if callback.data.endswith("miss") else "3 минуты")
    await state.update_data(draft=draft)
    await callback.answer()
    await show_builder(callback.message, draft, edit=True)


@router.callback_query(ChatStates.builder, F.data.startswith("chat:date:"))
async def set_date(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    draft["pending_date"] = callback.data.split("chat:date:")[1]
    await state.update_data(draft=draft)
    await callback.answer("поставлю перед следующим сообщением")
    await show_builder(callback.message, draft, edit=True)


@router.callback_query(ChatStates.wait_sticker, F.data.startswith("chat:setsticker:"))
async def set_sticker(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    _add(draft, kind="sticker", sticker=callback.data.split("chat:setsticker:")[1])
    await state.update_data(draft=draft)
    await state.set_state(ChatStates.builder)
    await callback.answer()
    await show_builder(callback.message, draft, edit=True)


@router.message(ChatStates.wait_sticker, F.text)
async def sticker_from_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    runs = [r[1] for r in chatgen.split_runs(message.text.strip()) if r[0] == "emoji"]
    if not runs:
        await message.answer("это не эмодзи. пришли один смайлик или выбери кнопкой")
        return
    _add(draft, kind="sticker", sticker=runs[0])
    await state.update_data(draft=draft)
    await state.set_state(ChatStates.builder)
    await show_builder(message, draft, state=state)


async def _finish_videonote(message: Message, state: FSMContext, file_id: Optional[str]) -> None:
    data = await state.get_data()
    draft = data["draft"]
    _add(draft, kind="videonote", photo=file_id, duration=random.choice(("0:06", "0:11", "0:24")))
    await state.update_data(draft=draft)
    await state.set_state(ChatStates.builder)
    await show_builder(message, draft, state=state)


@router.message(ChatStates.wait_photo_for, F.photo)
async def videonote_photo(message: Message, state: FSMContext) -> None:
    await _finish_videonote(message, state, message.photo[-1].file_id)


@router.message(ChatStates.wait_file_name, F.text)
async def file_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["draft"]
    _add(draft, kind="file", file_name=message.text.strip()[:40],
         file_size=random.choice(FILE_SIZES))
    await state.update_data(draft=draft)
    await state.set_state(ChatStates.builder)
    await show_builder(message, draft, state=state)


# ---------- отрисовка ----------

def after_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ дописать", callback_data="chat:more")],
        [InlineKeyboardButton(text="🆕 новая переписка", callback_data="chat:new")],
    ])


@router.callback_query(ChatStates.builder, F.data == "chat:render")
async def render(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    draft = data["draft"]
    if not draft["items"]:
        await callback.answer("переписка пустая, напиши хоть что-нибудь", show_alert=True)
        return
    await callback.answer("рисую")
    try:
        pages = await render_draft(bot, draft)
    except Exception:
        await callback.message.answer("не получилось собрать скрин, попробуй убрать последнее")
        raise

    caption = "мем-машина без вкуса и совести: @randomem_bot"
    if len(pages) == 1:
        # кнопка «в стикеры» живёт под самой картинкой: колбэк берёт file_id
        # прямо из сообщения, поэтому она работает и после перезапуска бота
        await callback.message.answer_photo(
            BufferedInputFile(pages[0].read(), filename="chat.jpg"), caption=caption,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[stickers.sticker_btn()]]))
    else:
        # в альбоме подпись показывается только у первой картинки
        media = [InputMediaPhoto(
            media=BufferedInputFile(b.read(), filename=f"chat{i}.jpg"),
            caption=caption if i == 0 else None)
            for i, b in enumerate(pages)]
        await callback.message.answer_media_group(media)
    await callback.message.answer(
        f"готово, {len(pages)} шт. листаются по порядку" if len(pages) > 1 else "готово",
        reply_markup=after_kb())


@router.callback_query(F.data == "chat:more")
async def render_more(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    if not draft:
        await callback.answer("переписка потерялась, начни заново", show_alert=True)
        return
    await state.set_state(ChatStates.builder)
    await callback.answer()
    await show_builder(callback.message, draft, state=state)


@router.callback_query(F.data == "chat:new")
async def render_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChatStates.settings)
    await state.update_data(draft=blank_draft())
    await callback.answer()
    await callback.message.answer("погнали заново. какой чат рисуем?", reply_markup=mode_kb())
