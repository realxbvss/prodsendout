import os
import logging
import sys
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.storage.redis import RedisStorage

from .youtube_service import YouTubeService
from src.utils import (
    load_dotenv,
    get_user_data,
    update_user_data,
    fernet,
    storage,
    REQUIRED_ENV
)

# Инициализация логирования и окружения
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("logs/bot.log")]
)

logger = logging.getLogger(__name__)
load_dotenv(Path(__file__).parent / ".env")

# Инициализация бота и хранилища
bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher(storage=storage)

# Основные команды
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    commands = """🎥 Available commands:
    /start - Main menu
    /auth - YouTube auth
    /upload - Upload content
    /guide - User guide"""
    await message.answer(commands)

@dp.message(Command("guide"))
async def cmd_guide(message: types.Message):
    guide_text = "📚 User guide content..."
    await message.answer(guide_text)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer("ℹ️ Help information...")

# Инициализация сервисов
youtube_service = YouTubeService(bot, dp)
youtube_service.setup_routes()

async def main():
    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logger.info("Получен сигнал отмены")
    finally:
        await storage.close()
        try:
            await bot.close()
        except Exception as e:
            logger.error(f"Ошибка при закрытии бота: {e}")

if __name__ == "__main__":
    if missing := [var for var in REQUIRED_ENV if not os.getenv(var)]:
        logger.critical(f"Missing env vars: {missing}")
        sys.exit(1)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    finally:
        loop.close()
        logger.info("Работа бота завершена")