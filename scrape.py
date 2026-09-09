"""
Скачивает все фото из указанного Telegram-канала под твоим личным
аккаунтом. При первом запуске один раз спросит номер телефона и код
подтверждения — дальше сессия сохраняется локально в файле
scraper_session.session, повторно логиниться не надо.
"""

from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto
import asyncio
import os

# --- впиши сюда свои данные с my.telegram.org ---
API_ID = 21859467                # число
API_HASH = "fae9af0322610ec324a66b0be4ecb72d"   # строка
CHANNEL = "slavicvibeforest"       # без @, например durov (не ссылка, не с собакой)
# --------------------------------------------------

OUTPUT_DIR = "downloads"

client = TelegramClient("scraper_session", API_ID, API_HASH)


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    manifest = []

    print("подключаюсь к Telegram...")
    async with client:
        print(f"ищу канал {CHANNEL}...")
        channel = await client.get_entity(CHANNEL)
        print("канал найден, начинаю сканировать историю...")

        count = 0
        scanned = 0
        async for message in client.iter_messages(channel):
            scanned += 1
            if scanned % 50 == 0:
                print(f"просмотрено сообщений: {scanned}, из них фото: {count}")

            if isinstance(message.media, MessageMediaPhoto):
                count += 1
                filename = f"{OUTPUT_DIR}/{message.id}.jpg"
                await message.download_media(file=filename)

                caption = (message.text or "").replace("\n", " ").strip()
                manifest.append(f"{message.id}.jpg\t{caption}")

                if count % 5 == 0:
                    print(f"скачано фото: {count}")

                await asyncio.sleep(0.3)  # не долбим API слишком часто

        with open(f"{OUTPUT_DIR}/manifest.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(manifest))

        print(f"готово! всего просмотрено сообщений: {scanned}, скачано фото: {count}")


if __name__ == "__main__":
    asyncio.run(main())

