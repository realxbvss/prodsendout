import os
import sys
import logging
import pickle
import asyncio
import tempfile
from pathlib import Path
from typing import Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.redis import RedisStorage
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from cryptography.fernet import Fernet
import redis.asyncio as redis
import httpx
from dotenv import load_dotenv
load_dotenv("EnvConfiguration.env")  # Укажите правильный путь
# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Конфигурация
CLIENT_SECRETS_FILE = "client_secrets.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"

# Инициализация
bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
storage = RedisStorage.from_url(os.getenv("REDIS_URL"))
dp = Dispatcher()
r = redis.from_url(os.getenv("REDIS_URL"))
fernet = Fernet(os.getenv("ENCRYPTION_KEY").encode())


async def save_credentials(user_id: int, credentials: Credentials):
    encrypted = fernet.encrypt(credentials.to_json().encode())
    await r.set(f"user:{user_id}:creds", encrypted)


async def load_credentials(user_id: int) -> Optional[Credentials]:
    data = await r.get(f"user:{user_id}:creds")
    if not data: return None
    return Credentials.from_json(fernet.decrypt(data).decode())


@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "🎥 YouTube Upload Bot\n\n"
        "🔑 Для загрузки видео выполните /auth"
    )


@dp.message(Command("auth"))
async def auth(message: types.Message):
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )
    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline"
    )
    await r.set(f"flow:{message.from_user.id}", pickle.dumps(flow))
    await message.answer(f"🔗 Авторизуйтесь:\n{auth_url}\nВведите код командой /code [ВАШ_КОД]")


@dp.message(Command("code"))
async def code(message: types.Message):
    try:
        flow = pickle.loads(await r.get(f"flow:{message.from_user.id}"))
        flow.fetch_token(code=message.text.split()[-1])
        await save_credentials(message.from_user.id, flow.credentials)
        await message.answer("✅ Авторизация успешна! Теперь можно загружать видео командой /upload")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


@dp.message(Command("upload"))
async def upload(message: types.Message):
    # Логика загрузки видео через микросервис
    await message.answer("🔄 Загрузка начата...")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())