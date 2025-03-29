import os
import logging
import sys
import asyncio
from vpn_manager import VPNManager
from datetime import datetime, timezone
from pathlib import Path
from aiogram.fsm.state import State, StatesGroup
from instagrapi import Client
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from cryptography.fernet import Fernet

from .youtube_service import YouTubeService
from .instagram_service import InstagramService
from src.utils import (
    load_dotenv,
    get_user_data,
    update_user_data,
    fernet,
    storage,
    REQUIRED_ENV
)

# region [ CONFIGURATION ]
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("logs/bot.log")]
)

logger = logging.getLogger(__name__)
load_dotenv(Path(__file__).parent / ".env")

bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher(storage=storage)


# endregion

# region [ COMMAND HANDLERS ]
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Обработчик команды /start с улучшенным оформлением"""
    welcome_text = (
        "🌟 <b>Добро пожаловать в Producer Sends Out Bot!</b>\n\n"
        "📌 <i>Доступные команды:</i>\n"
        "▫️ /start - Главное меню\n"
        "▫️ /guide - Полное руководство\n"
        "▫️ /youtube - Работа с YouTube\n"
        "▫️ /instagram - Работа с Instagram\n"
        "▫️ /help - Техническая поддержка\n\n"
        "Выберите платформу для работы:"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="YouTube 🎥", callback_data="youtube_main"),
            InlineKeyboardButton(text="Instagram 📸", callback_data="instagram_main")
        ],
        [InlineKeyboardButton(text="Инструкция 📖", callback_data="guide")]
    ])

    await message.answer(welcome_text, reply_markup=keyboard)


@dp.message(Command("guide"))
async def cmd_guide(message: types.Message):
    """Расширенное руководство пользователя"""
    guide_text = (
        "<b>📚 Полное руководство по использованию бота</b>\n\n"
        "<u>YouTube функции:</u>\n"
        "1. Авторизация: /auth\n"
        "2. Загрузка видео: /upload\n"
        "3. Управление каналами: /channels\n\n"
        "<u>Instagram функции:</u>\n"
        "1. Авторизация: /instagram auth\n"
        "2. Анализ сообщений: /instagram messages\n"
        "3. Статистика: /instagram stats\n\n"
        "⚙️ Настройки:\n"
        "- Смена языка: /language\n"
        "- Помощь: /help"
    )
    await message.answer(guide_text)


@dp.message(Command("instagram"))
async def cmd_instagram(message: types.Message):
    """Главное меню Instagram"""
    insta_text = (
        "📸 <b>Instagram Менеджер</b>\n\n"
        "Выберите действие:\n"
        "1. Авторизация - /instagram_auth\n"
        "2. Анализ сообщений - /instagram_msgs\n"
        "3. Статистика аккаунта - /instagram_stats\n"
        "4. Публикация контента - /instagram_post"
    )
    await message.answer(insta_text)


# endregion

# region [ SERVICE INITIALIZATION ]
youtube_service = YouTubeService(bot, dp)
instagram_service = InstagramService(bot, dp)


def setup_services():
    """Инициализация сервисов"""
    youtube_service.setup_routes()
    instagram_service.setup_routes()

    # Дополнительные обработчики для Instagram
    dp.callback_query.register(
        handle_instagram_callback,
        F.data.startswith("instagram_")
    )


async def handle_instagram_callback(callback: types.CallbackQuery):
    """Обработчик callback-ов для Instagram"""
    action = callback.data.split("_")[1]

    match action:
        case "auth":
            await instagram_service.handle_auth_start(callback.message)
        case "stats":
            await callback.answer("Статистика в разработке 🛠")
        case _:
            await callback.answer("Неизвестное действие")


# endregion

# region [ SHUTDOWN HANDLERS ]
async def graceful_shutdown():
    """Корректное завершение работы"""
    logger.info("Завершение работы...")
    await storage.close()

    try:
        await bot.session.close()
    except Exception as e:
        logger.error(f"Ошибка закрытия сессии: {e}")

    try:
        await bot.close()
    except TelegramRetryAfter as e:
        logger.warning(f"Flood Control: пропуск ожидания {e.retry_after} сек.")
    except Exception as e:
        logger.error(f"Ошибка закрытия бота: {e}")


# endregion

# region [ MAIN EXECUTION ]
async def main():
    """Основная функция запуска бота"""
    vpn = VPNManager()
    await vpn.connect()
    setup_services()

    try:
        await dp.start_polling(bot, handle_as_tasks=False)
    except asyncio.CancelledError:
        pass
    finally:
        await graceful_shutdown()

class ProxyStates(StatesGroup):
    waiting_proxy = State()

@dp.message(Command("set_proxy"))
async def cmd_set_proxy(message: Message, state: FSMContext):
    await message.answer(
        "🔧 Отправьте прокси в формате:\n"
        "<code>тип://логин:пароль@хост:порт</code>\n"
        "Пример: <code>socks5://user:pass@127.0.0.1:9050</code>"
    )
    await state.set_state(ProxyStates.waiting_proxy)

@dp.message(ProxyStates.waiting_proxy)
async def handle_proxy_input(message: Message, state: FSMContext):
    try:
        # Шифрование и сохранение
        encrypted = Fernet(os.getenv("ENCRYPTION_KEY")).encrypt(message.text.encode())
        await storage.redis.set(f"proxy:{message.from_user.id}", encrypted)
        await message.answer("✅ Прокси успешно сохранен!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
    await state.clear()


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
# endregion