import os
import sys
import logging
import tempfile
import subprocess
import asyncio
import signal
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from cryptography.fernet import Fernet

# ================== ИНИЦИАЛИЗАЦИЯ ==================
Path("temp").mkdir(exist_ok=True)
Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/youtube_bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

REQUIRED_ENV = ["TELEGRAM_TOKEN", "REDIS_URL", "ENCRYPTION_KEY"]
if missing := [var for var in REQUIRED_ENV if not os.getenv(var)]:
    logger.critical(f"Missing environment variables: {missing}")
    sys.exit(1)

try:
    storage = RedisStorage.from_url(
        os.getenv("REDIS_URL"),
        connection_kwargs={
            "retry_on_timeout": True,
            "socket_connect_timeout": 5,
            "health_check_interval": 30
        }
    )
    logger.info("Redis подключен")
except Exception as e:
    logger.critical(f"Redis error: {str(e)}")
    sys.exit(1)

bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher(storage=storage)
fernet = Fernet(os.getenv("ENCRYPTION_KEY").encode())


# ================== СОСТОЯНИЯ ==================
class UploadStates(StatesGroup):
    CONTENT_TYPE = State()
    MEDIA_UPLOAD = State()
    METADATA = State()
    VPN_CONFIG = State()
    PROXY = State()
    YOUTUBE_TOKEN = State()
    OAUTH_FLOW = State()


# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
async def get_user_data(user_id: int) -> Dict:
    try:
        data = await storage.redis.hgetall(f"user:{user_id}")
        return {k.decode(): v.decode() for k, v in data.items()}
    except Exception as e:
        logger.error(f"Redis error: {e}")
        return {}


async def update_user_data(user_id: int, data: Dict) -> None:
    try:
        await storage.redis.hset(f"user:{user_id}", mapping=data)
    except Exception as e:
        logger.error(f"Redis update error: {e}")


async def acquire_lock() -> bool:
    try:
        return await storage.redis.set("bot_lock", "1", nx=True, ex=60)
    except Exception as e:
        logger.error(f"Lock error: {e}")
        return False


async def release_lock():
    try:
        await storage.redis.delete("bot_lock")
    except Exception as e:
        logger.error(f"Unlock error: {e}")


async def run_subprocess(cmd: list) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.wait()
        return proc.returncode == 0
    except Exception as e:
        logger.error(f"Subprocess error: {e}")
        return False


async def decrypt_user_data(user_id: int, key: str) -> Optional[bytes]:
    try:
        user_data = await get_user_data(user_id)
        if encrypted := user_data.get(key):
            return fernet.decrypt(encrypted.encode())
        return None
    except Exception as e:
        logger.error(f"Decryption error: {e}")
        return None


# ================== ОБРАБОТЧИКИ КОМАНД ==================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    try:
        # Исправление ошибки: правильные параметры для set_state
        await storage.set_state(
            chat=message.chat.id,
            user=message.from_user.id,
            state=None
        )

        credentials = await get_valid_credentials(message.from_user.id)
        token_status = ""

        if credentials:
            expiry_time = credentials.expiry.replace(tzinfo=None)
            time_left = expiry_time - datetime.utcnow()

            if time_left.total_seconds() > 0:
                token_status = (
                    "\n\n🔐 Статус авторизации: "
                    f"Действителен еще {time_left // timedelta(hours=1)} ч. "
                    f"{(time_left % timedelta(hours=1)) // timedelta(minutes=1)} мин."
                )
            else:
                token_status = "\n\n⚠️ Токен истек! Используйте /auth"

        response = (
            "🎥 <b>YouTube Upload Bot</b>\n\n"
            "📚 Основные команды:\n"
            "▶️ /upload - Начать загрузку\n"
            "🔑 /auth - Авторизация\n"
            "⚙️ /view_configs - Конфигурации\n"
            "🗑️ /delete_config &lt;ключ&gt; - Удалить\n\n"
            f"{token_status}"
        )
        await message.answer(response, parse_mode="HTML")

    except Exception as e:
        logger.error(f"/start error: {e}", exc_info=True)
        await message.answer("⚠️ Ошибка. Попробуйте позже.")


@dp.message(Command("auth"))
async def cmd_auth(message: types.Message, state: FSMContext):
    await message.answer("📤 Отправьте client_secrets.json")
    await state.set_state(UploadStates.OAUTH_FLOW)


@dp.message(UploadStates.OAUTH_FLOW, F.document)
async def handle_oauth_file(message: types.Message, state: FSMContext, bot: Bot):
    try:
        file = await bot.get_file(message.document.file_id)
        path = Path("temp") / f"{message.from_user.id}_secrets.json"
        await bot.download_file(file.file_path, path)

        flow = InstalledAppFlow.from_client_secrets_file(
            str(path),
            scopes=["https://www.googleapis.com/auth/youtube.upload"],
            redirect_uri="urn:ietf:wg:oauth:2.0:oob"
        )
        auth_url, _ = flow.authorization_url(prompt="consent")

        # Сохраняем всю конфигурацию
        await state.update_data(
            client_config=flow.client_config,
            flow=flow.serialize()
        )
        await message.answer(f"🔑 Авторизуйтесь: {auth_url}")
        path.unlink()

    except Exception as e:
        logger.error(f"OAuth file error: {e}")
        await message.answer("❌ Неверный формат файла!")


@dp.message(UploadStates.OAUTH_FLOW)
async def handle_oauth_code(message: types.Message, state: FSMContext):
    try:
        code = message.text.strip()
        data = await state.get_data()

        # Исправление ошибки: правильное восстановление потока
        flow = InstalledAppFlow.from_client_config(
            data['client_config'],
            scopes=["https://www.googleapis.com/auth/youtube.upload"]
        )
        flow.fetch_token(code=code)
        credentials = flow.credentials

        token_data = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'expiry': credentials.expiry.isoformat(),
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'token_uri': credentials.token_uri,
            'scopes': credentials.scopes
        }
        encrypted = fernet.encrypt(json.dumps(token_data).encode())
        await update_user_data(message.from_user.id, {'youtube_token': encrypted.decode()})

        await message.answer("✅ Авторизация успешна!")
        await state.clear()

    except Exception as e:
        logger.error(f"OAuth code error: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")


@dp.message(Command("view_configs"))
async def cmd_view_configs(message: types.Message):
    try:
        user_data = await get_user_data(message.from_user.id)
        configs = [f"🔑 {key}" for key in user_data if key.startswith(("vpn:", "proxy"))]

        if configs:
            await message.answer("📂 Конфигурации:\n" + "\n".join(configs))
        else:
            await message.answer("❌ Нет конфигураций")
    except Exception as e:
        logger.error(f"View configs error: {e}")
        await message.answer("⚠️ Ошибка")


@dp.message(Command("delete_config"))
async def cmd_delete_config(message: types.Message):
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("❌ Укажите ключ")
            return

        config_key = args[1]
        await storage.redis.hdel(f"user:{message.from_user.id}", config_key)
        await message.answer(f"✅ Удалено: {config_key}")

    except Exception as e:
        logger.error(f"Delete config error: {e}")
        await message.answer("⚠️ Ошибка")


# ================== ЗАГРУЗКА ВИДЕО ==================
async def get_valid_credentials(user_id: int) -> Optional[Credentials]:
    try:
        encrypted = await decrypt_user_data(user_id, "youtube_token")
        if not encrypted:
            return None

        token_data = json.loads(encrypted.decode())
        expiry = datetime.fromisoformat(token_data['expiry'])

        if datetime.utcnow() > expiry - timedelta(minutes=5):
            credentials = Credentials(
                token=token_data['token'],
                refresh_token=token_data['refresh_token'],
                token_uri=token_data['token_uri'],
                client_id=token_data['client_id'],
                client_secret=token_data['client_secret'],
                scopes=token_data['scopes']
            )
            credentials.refresh(Request())

            token_data.update({
                'token': credentials.token,
                'expiry': credentials.expiry.isoformat()
            })
            encrypted = fernet.encrypt(json.dumps(token_data).encode())
            await update_user_data(user_id, {'youtube_token': encrypted.decode()})

        return Credentials(**token_data)
    except Exception as e:
        logger.error(f"Credentials error: {e}")
        return None


async def shutdown(signal, loop):
    logger.info("Завершение работы...")
    await release_lock()
    await bot.close()
    await storage.close()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


async def main():
    try:
        if not await acquire_lock():
            logger.error("Бот уже запущен!")
            sys.exit(1)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(sig, loop)))

        logger.info("Бот запущен")
        await dp.start_polling(bot)

    except Exception as e:
        logger.critical(f"Critical error: {e}", exc_info=True)
    finally:
        await release_lock()
        logger.info("Ресурсы освобождены")


if __name__ == "__main__":
    asyncio.run(main())