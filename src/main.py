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
from fastapi import FastAPI

# ================== ИНИЦИАЛИЗАЦИЯ ==================
# Создание директорий
Path("temp").mkdir(exist_ok=True)
Path("logs").mkdir(exist_ok=True)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/youtube_bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

# Проверка переменных
REQUIRED_ENV = ["TELEGRAM_TOKEN", "REDIS_URL", "ENCRYPTION_KEY"]
if missing := [var for var in REQUIRED_ENV if not os.getenv(var)]:
    logger.critical(f"Missing environment variables: {missing}")
    sys.exit(1)

# Инициализация Redis с обработкой ошибок
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

# Инициализация компонентов
bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher(storage=storage)
fernet = Fernet(os.getenv("ENCRYPTION_KEY").encode())
app = FastAPI()


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
def validate_metadata(metadata: Dict) -> bool:
    required_fields = ['title', 'description', 'tags']
    return all(field in metadata for field in required_fields)


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
        credentials = await get_valid_credentials(message.from_user.id)
        token_status = ""

        if credentials:
            expiry_time = credentials.expiry.replace(tzinfo=None)
            time_left = expiry_time - datetime.utcnow()

            if time_left.total_seconds() > 0:
                token_status = (
                    "\n\n🔐 Статус авторизации: "
                    f"Действителен еще {time_left // timedelta(hours=1)} ч."
                )
            else:
                token_status = "\n\n⚠️ Токен истек! Используйте /auth"

        await message.answer(
            "🎥 <b>YouTube Upload Bot</b>\n\n"
            "📚 Основные команды:\n"
            "▶️ /upload - Начать загрузку\n"
            "🔑 /auth - Авторизация\n"
            "📖 /guide - Инструкция\n"
            "⚙️ /view_configs - Конфигурации\n"
            "🗑️ /delete_config <ключ> - Удалить\n\n"
            f"{token_status}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"/start error: {e}")


@dp.message(Command("guide"))
async def cmd_guide(message: types.Message):
    guide_text = (
        "📘 <b>Инструкция:</b>\n\n"
        "1. Создайте проект в Google Cloud Console\n"
        "2. Включите YouTube Data API v3\n"
        "3. Скачайте client_secrets.json\n"
        "4. Отправьте файл боту через /auth"
    )
    await message.answer(guide_text, parse_mode="HTML")


@dp.message(Command("auth"))
async def cmd_auth(message: types.Message, state: FSMContext):
    await message.answer("📤 Отправьте client_secrets.json")
    await state.set_state(UploadStates.OAUTH_FLOW)


@dp.message(UploadStates.OAUTH_FLOW, F.document)
async def handle_oauth_file(message: types.Message, state: FSMContext):
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
        await state.update_data(client_config=flow.client_config)
        await message.answer(f"🔑 Авторизуйтесь: {auth_url}")
        path.unlink()

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error(f"OAuth file error: {e}")


@dp.message(UploadStates.OAUTH_FLOW)
async def handle_oauth_code(message: types.Message, state: FSMContext):
    try:
        code = message.text.strip()
        data = await state.get_data()
        flow = InstalledAppFlow.from_client_config(
            data['client_config'],
            scopes=["https://www.googleapis.com/auth/youtube.upload"],
            redirect_uri="urn:ietf:wg:oauth:2.0:oob"
        )
        flow.fetch_token(code=code)
        credentials = flow.credentials

        token_data = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'expiry': credentials.expiry.isoformat()
        }
        encrypted = fernet.encrypt(json.dumps(token_data).encode())
        await update_user_data(message.from_user.id, {'youtube_token': encrypted.decode()})

        await message.answer("✅ Авторизация успешна!")
        await state.clear()

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error(f"OAuth code error: {e}")


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


# ================== ЗАГРУЗКА ВИДЕО ==================
async def create_video_from_media(image_path: str, audio_path: str) -> str:
    output_path = tempfile.mktemp(suffix=".mp4", dir="temp")
    cmd = [
        "ffmpeg",
        "-loop", "1",
        "-i", image_path,
        "-i", audio_path,
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-c:a", "aac",
        "-shortest",
        "-y", output_path
    ]
    if not await run_subprocess(cmd):
        raise Exception("Ошибка создания видео")
    return output_path


async def upload_video(service, video_path: str, metadata: Dict) -> str:
    request_body = {
        "snippet": {
            "title": metadata['title'],
            "description": metadata['description'],
            "tags": metadata['tags'],
            "categoryId": "22"
        },
        "status": {"privacyStatus": "public"}
    }
    media_file = MediaFileUpload(video_path, resumable=True)
    request = service.videos().insert(
        part="snippet,status",
        body=request_body,
        media_body=media_file
    )
    return (await asyncio.to_thread(request.execute))['id']


# ================== ОСНОВНОЙ ПРОЦЕСС ==================
async def start_upload_process(message: types.Message, state: FSMContext):
    try:
        state_data = await state.get_data()
        user_data = await get_user_data(message.from_user.id)

        # VPN подключение
        for key in user_data:
            if key.startswith("vpn:"):
                vpn_data = await decrypt_user_data(message.from_user.id, key)
                vpn_type = key.split(":")[1]
                with tempfile.NamedTemporaryFile(delete=False) as vpn_file:
                    vpn_file.write(vpn_data)
                    vpn_path = vpn_file.name

                cmd = ["openvpn", "--config", vpn_path] if vpn_type == "openvpn" else ["wg-quick", "up", vpn_path]
                if not await run_subprocess(cmd):
                    raise Exception("VPN ошибка")
                Path(vpn_path).unlink()

        # Создание видео
        if state_data.get('content_type') == 'audio_image':
            video_path = await create_video_from_media(
                state_data['image_path'],
                state_data['audio_path']
            )
        else:
            video_path = state_data['video_path']

        # Загрузка на YouTube
        credentials = await get_valid_credentials(message.from_user.id)
        service = await asyncio.to_thread(
            build, "youtube", "v3", credentials=credentials
        )
        video_id = await upload_video(service, video_path, state_data['metadata'])
        await message.answer(f"✅ Видео загружено! ID: {video_id}")

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error("Upload error", exc_info=True)
    finally:
        for file in ['video_path', 'audio_path', 'image_path']:
            if path := state_data.get(file):
                Path(path).unlink(missing_ok=True)
        await state.clear()


# ================== ЗАВЕРШЕНИЕ РАБОТЫ ==================
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