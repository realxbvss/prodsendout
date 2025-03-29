import os
import logging
import sys
import asyncio
from instagrapi import Client
from .instagram_service import InstagramService
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.exceptions import TelegramRetryAfter

from .youtube_service import YouTubeService
from src.utils import (
    load_dotenv,
    get_user_data,
    update_user_data,
    fernet,
    storage,
    REQUIRED_ENV
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("logs/bot.log")]
)

logger = logging.getLogger(__name__)
load_dotenv(Path(__file__).parent / ".env")

bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher(storage=storage)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("🎥 Доступные команды: /start, /auth, /upload, /guide")


@dp.message(Command("guide"))
async def cmd_guide(message: types.Message):
    await message.answer("📚 Инструкция по использованию бота...")

# --- ИНИЦИАЛИЗАЦИЯ СЕРВИСОВ ---

instagram_service = InstagramService(bot, dp)
youtube_service = YouTubeService(bot, dp)
youtube_service.setup_routes()

async def graceful_shutdown():
    logger.info("Завершение работы...")
    await storage.close()

    try:
        # Закрываем сессию бота без ожидания
        await bot.session.close()
    except Exception as e:
        logger.error(f"Ошибка закрытия сессии: {e}")

    try:
        # Принудительное закрытие без обработки Flood Control
        await bot.close()
    except TelegramRetryAfter as e:
        logger.warning(f"Режим разработки: пропуск ожидания {e.retry_after} сек.")
    except Exception as e:
        logger.error(f"Ошибка закрытия бота: {e}")


async def main():
    try:
        await dp.start_polling(bot, handle_as_tasks=False)
    except asyncio.CancelledError:
        pass
    finally:
        await graceful_shutdown()


if __name__ == "__main__":
    if missing := [var for var in REQUIRED_ENV if not os.getenv(var)]:
        logger.critical(f"Отсутствуют переменные: {missing}")
        sys.exit(1)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Остановка по запросу пользователя")
    finally:
        tasks = asyncio.all_tasks(loop=loop)
        for t in tasks:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        logger.info("Работа завершена корректно")