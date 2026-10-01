"""Общие элементы интерфейса бота.

Кнопка отмены лежит здесь, а не в bot.py, потому что её текст нужен сразу трём
модулям: bot.py ловит нажатие глобальным хендлером, а сценарии переписки и
стикеров показывают её, пока держат состояние. Раньше текст дублировался
вручную с комментарием «обязан совпадать» — достаточно было опечатки, чтобы
выход из сценария перестал работать.
"""

from aiogram import Bot
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_CANCEL = "✖️ Отмена"

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)


_BOT_CAPTION: str | None = None


async def bot_caption(bot: Bot) -> str:
    """Подпись под результатом без захардкоженного username бота."""
    global _BOT_CAPTION
    if _BOT_CAPTION is None:
        try:
            username = (await bot.get_me()).username
        except Exception:
            return "мем-машина без вкуса и совести"
        suffix = f": @{username}" if username else ""
        _BOT_CAPTION = f"мем-машина без вкуса и совести{suffix}"
    return _BOT_CAPTION
