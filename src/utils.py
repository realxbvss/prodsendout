import asyncio
import json
import os
from typing import Optional
from aiogram.fsm.storage.redis import RedisStorage
from cryptography.fernet import Fernet
from dotenv import load_dotenv

import logging
logger = logging.getLogger(__name__)

load_dotenv()

REQUIRED_ENV = ["TELEGRAM_TOKEN", "REDIS_URL", "ENCRYPTION_KEY"]
fernet = Fernet(os.getenv("ENCRYPTION_KEY").encode())

storage = RedisStorage.from_url(
    os.getenv("REDIS_URL"),
    connection_kwargs={
        "socket_connect_timeout": 5,
        "retry_on_timeout": True
    }
)

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
        logger.error(f"Ошибка подпроцесса: {str(e)}")
        return False

async def get_instagram_session(user_id: int) -> Optional[dict]:
    encrypted = await get_user_data(user_id, "instagram_session")
    if not encrypted:
        return None
    return json.loads(fernet.decrypt(encrypted.encode()).decode())

async def decrypt_user_data(user_id: int, key: str) -> Optional[bytes]:
    try:
        user_data = await get_user_data(user_id)
        if encrypted := user_data.get(key):
            return fernet.decrypt(encrypted.encode())
        return None
    except Exception as e:
        logger.error(f"Ошибка дешифрования: {str(e)}")
        return None

async def get_user_data(user_id: int, key: str) -> Optional[bytes]:
    data = await storage.redis.hget(f"user:{user_id}", key)
    return fernet.decrypt(data) if data else None

async def update_user_data(user_id: int, key: str, value: str):
    encrypted = fernet.encrypt(value.encode())
    await storage.redis.hset(f"user:{user_id}", key, encrypted)