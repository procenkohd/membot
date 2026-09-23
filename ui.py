"""Общие элементы интерфейса бота.

Кнопка отмены лежит здесь, а не в bot.py, потому что её текст нужен сразу трём
модулям: bot.py ловит нажатие глобальным хендлером, а сценарии переписки и
стикеров показывают её, пока держат состояние. Раньше текст дублировался
вручную с комментарием «обязан совпадать» — достаточно было опечатки, чтобы
выход из сценария перестал работать.
"""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_CANCEL = "✖️ Отмена"

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)
