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
from aiogram.fsm.storage.base import StorageKey  # Добавить в импорты
from aiogram.exceptions import TelegramBadRequest
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

from moviepy.editor import ImageClip, AudioFileClip

from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

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
    logger.critical(f"Отсутствуют переменные окружения: {missing}")
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
    logger.info("Успешное подключение к Redis")
except Exception as e:
    logger.critical(f"Ошибка Redis: {str(e)}")
    sys.exit(1)

bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher(storage=storage)
fernet = Fernet(os.getenv("ENCRYPTION_KEY").encode())


# ================== СОСТОЯНИЯ ==================
class UploadStates(StatesGroup):
    OAUTH_FLOW = State()
    CONTENT_TYPE = State()        # Выбор типа контента
    MEDIA_UPLOAD = State()        # Загрузка готового видео
    PHOTO_UPLOAD = State()        # Загрузка фото
    AUDIO_UPLOAD = State()        # Загрузка аудио
    VIDEO_GENERATION = State()    # Генерация видео из фото+MP3
    VPN_CONFIG = State()          # Настройка VPN для канала
    CHANNEL_SELECT = State()      # Выбор канала
    MULTI_CHANNEL = State()       # Выбор количества каналов (1-10)


# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================

# ================== ЗАГРУЗКА ВИДЕО ==================
async def upload_video(user_id: int, video_path: str, title: str, description: str) -> str:
    try:
        # Получение токена
        credentials = await get_valid_credentials(user_id)
        if not credentials:
            raise ValueError("❌ Токен не найден. Выполните /auth.")

        # Создание клиента YouTube
        youtube = build("youtube", "v3", credentials=credentials)

        # Загрузка видео
        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title,
                    "description": description,
                    "categoryId": "22"  # Категория "People & Blogs"
                },
                "status": {
                    "privacyStatus": "private"  # или "public", "unlisted"
                }
            },
            media_body=MediaFileUpload(video_path)
        )
        response = request.execute()
        return response["id"]

    except Exception as e:
        logger.error(f"Ошибка загрузки видео: {str(e)}", exc_info=True)
        raise

async def get_user_data(user_id: int) -> Dict:
    try:
        data = await storage.redis.hgetall(f"user:{user_id}")
        return {k.decode(): v.decode() for k, v in data.items()}
    except Exception as e:
        logger.error(f"Ошибка Redis: {str(e)}")
        return {}


async def update_user_data(user_id: int, data: Dict) -> None:
    try:
        await storage.redis.hset(f"user:{user_id}", mapping=data)
        logger.info(f"Данные пользователя {user_id} обновлены.")
    except Exception as e:
        logger.error(f"Ошибка Redis: {str(e)}")

async def acquire_lock() -> bool:
    try:
        return await storage.redis.set("bot_lock", "1", nx=True, ex=60)
    except Exception as e:
        logger.error(f"Ошибка блокировки: {str(e)}")
        return False


async def release_lock():
    try:
        await storage.redis.delete("bot_lock")
    except Exception as e:
        logger.error(f"Ошибка разблокировки: {str(e)}")


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


async def decrypt_user_data(user_id: int, key: str) -> Optional[bytes]:
    try:
        user_data = await get_user_data(user_id)
        if encrypted := user_data.get(key):
            return fernet.decrypt(encrypted.encode())
        return None
    except Exception as e:
        logger.error(f"Ошибка дешифрования: {str(e)}")
        return None


# ================== ОБРАБОТЧИКИ КОМАНД ==================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    try:
        key = StorageKey(
            chat_id=message.chat.id,  # ID чата
            user_id=message.from_user.id,  # ID пользователя
            bot_id=bot.id  # ID бота
        )
        await storage.set_state(key=key, state=None)

        credentials = await get_valid_credentials(message.from_user.id)
        token_status = ""

        if credentials:
            from datetime import datetime, timezone
            expiry_time = credentials.expiry.replace(tzinfo=timezone.utc)
            time_left = expiry_time - datetime.now(timezone.utc)

            if time_left.total_seconds() > 0:
                token_status = (
                    f"\n\n🔐 Статус авторизации: Действителен еще "
                    f"{time_left // timedelta(hours=1)} ч. "
                    f"{(time_left % timedelta(hours=1)) // timedelta(minutes=1)} мин."
                )
            else:
                token_status = "\n\n⚠️ Токен истек! Используйте /auth"

        response = (
            "🎥 <b>YouTube Upload Bot</b>\n\n"
            "📚 Основные команды:\n"
            "⚙️ /guide - Показать инструкцию\n"
            "▶️ /upload - Начать загрузку видео\n"
            "🔑 /auth - Авторизация в YouTube\n"
            "⚙️ /view_configs - Показать сохраненные настройки\n"
            "🗑️ /delete_config &lt;ключ&gt; - Удалить конфигурацию\n\n"
            "❗️ <b>Перед использованием /upload необходимо выполнить /auth</b>"
            f"{token_status}"
        )
        await message.answer(response, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Ошибка /start: {str(e)}", exc_info=True)
        await message.answer("⚠️ Произошла ошибка. Попробуйте позже.")


@dp.message(Command("auth"))
async def cmd_auth(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("📤 Отправьте файл client_secrets.json.")
    await state.set_state(UploadStates.OAUTH_FLOW)

# Добавьте в код (заглушки):
async def get_user_channels(user_id: int) -> list:
    return []  # Реализуйте логику

async def get_vpn_for_channel(user_id: int, channel_id: str) -> str:
    encrypted = await storage.redis.hget(f"user:{user_id}", f"vpn:{channel_id}")
    return fernet.decrypt(encrypted).decode() if encrypted else ""

def connect_to_vpn(config: str):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ovpn") as f:
        f.write(config.encode())
        subprocess.run(
            ["sudo", "openvpn", "--config", f.name],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

async def save_channel(user_id: int, channel_name: str, channel_id: str):
    await storage.redis.hset(f"user:{user_id}:channels", channel_id, channel_name)

async def get_user_channels(user_id: int) -> list:
    channels = await storage.redis.hgetall(f"user:{user_id}:channels")
    return [(k.decode(), v.decode()) for k, v in channels.items()]


@dp.message(Command("setup_channels"))
async def cmd_setup_channels(message: Message, state: FSMContext):
    # Получаем каналы через API
    channels = await get_youtube_channels(message.from_user.id)

    if not channels:
        await message.answer("❌ Нет доступных каналов. Сначала выполните /auth")
        return

    # Сохраняем каналы
    await storage.redis.hset(
        f"user:{message.from_user.id}:channels",
        mapping={channel_id: name for channel_id, name in channels}
    )

    await message.answer(
        "✅ Ваши каналы автоматически получены!\n"
        "Теперь загрузите VPN-конфиги для каждого канала через /setup_vpn"
    )


@dp.message(Command("setup_vpn"))
async def cmd_setup_vpn(message: Message, state: FSMContext):
    channels = await get_user_channels(message.from_user.id)
    if not channels:
        await message.answer("❌ Сначала настройте каналы через /setup_channels")
        return

    await state.update_data(channels=channels, current_channel=0)
    await ask_for_vpn_config(message, state)


async def ask_for_vpn_config(message: Message, state: FSMContext):
    data = await state.get_data()
    current = data["current_channel"] + 1
    channels = data["channels"]

    if current <= len(channels):
        channel_id, channel_name = channels[current - 1]
        await message.answer(
            f"🔐 Отправьте VPN-конфиг для канала: {channel_name}\n"
            f"(Используйте файл в формате .ovpn)"
        )
        await state.update_data(current_channel=current, current_channel_id=channel_id)
    else:
        await message.answer("✅ Все VPN-конфиги успешно сохранены!")
        await state.clear()

@dp.message(UploadStates.OAUTH_FLOW, F.document)
async def handle_oauth_file(message: types.Message, state: FSMContext, bot: Bot):
    path = None
    try:
        if message.document.mime_type != "application/json":
            await message.answer("❌ Файл должен быть в формате JSON.")
            return

        logger.info(f"Пользователь {message.from_user.id} отправил client_secrets.json")

        file = await bot.get_file(message.document.file_id)
        path = Path("temp") / f"{message.from_user.id}_client_secrets.json"
        await bot.download_file(file.file_path, path)

        if not path.exists():
            logger.error("Файл не сохранен!")
            await message.answer("❌ Ошибка при сохранении файла.")
            return

        with open(path, "r") as f:
            data = json.load(f)
            logger.debug(f"Содержимое файла: {json.dumps(data, indent=2)}")

        # Изменено: проверяем секцию "installed"
        if "installed" not in data:
            await message.answer("❌ В файле отсутствует секция 'installed'. Используйте Desktop-приложение.")
            return

        installed_data = data["installed"]  # Изменено: обращаемся к "installed"
        required_fields = ["client_id", "client_secret", "redirect_uris"]
        if not all(field in installed_data for field in required_fields):
            await message.answer("❌ Неверный формат файла. Скачайте client_secrets.json для Desktop.")
            return

        # Создание OAuth-потока
        flow = InstalledAppFlow.from_client_secrets_file(
            str(path),
            scopes=["https://www.googleapis.com/auth/youtube.upload"],
            redirect_uri="urn:ietf:wg:oauth:2.0:oob"
        )
        auth_url, _ = flow.authorization_url(prompt="consent")  # Определяем auth_url здесь

        await state.update_data(
            client_config=data["installed"],  # Важно!
            scopes=["https://www.googleapis.com/auth/youtube.upload"],
            redirect_uri="urn:ietf:wg:oauth:2.0:oob"
        )

        await message.answer(
            f"🔑 Авторизуйтесь по ссылке: {auth_url}\n\n"
            "После авторизации скопируйте код и отправьте его боту."
        )

    except json.JSONDecodeError:
        logger.error("Файл не является JSON")
        await message.answer("❌ Файл поврежден.")
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}", exc_info=True)
        await message.answer("❌ Не удалось обработать файл.")
    finally:
        if path:
            path.unlink(missing_ok=True)

async def generate_video(user_id: int, state: FSMContext):
    data = await state.get_data()
    output_path = Path("temp") / f"{user_id}_video.mp4"

    try:
        audio = AudioFileClip(data["audio_path"])
        clip = ImageClip(data["photo_path"]).set_duration(audio.duration)
        clip = clip.set_audio(audio)
        clip.write_videofile(str(output_path), fps=24)
        await state.update_data(video_path=str(output_path))
        await bot.send_message(
            user_id,
            "✅ Видео готово!\n"
            "1. Чтобы выбрать канал, используйте /channel_select\n"
            "2. Для настройки новых каналов: /setup_channels"
        )
        await state.set_state(UploadStates.CHANNEL_SELECT)
    except Exception as e:
        await bot.send_message(user_id, f"❌ Ошибка: {str(e)}")

@dp.message(Command("channel_select"))
async def cmd_channel_select(message: Message):
    await handle_channel_select(message, None)

@dp.message(UploadStates.PHOTO_UPLOAD, F.photo)
async def handle_photo(message: Message, state: FSMContext, bot: Bot):
    file = await bot.get_file(message.photo[-1].file_id)
    path = Path("temp") / f"{message.from_user.id}_photo.jpg"
    await bot.download_file(file.file_path, path)
    await state.update_data(photo_path=str(path))
    await message.answer("🎵 Теперь отправьте MP3-аудио:")
    await state.set_state(UploadStates.AUDIO_UPLOAD)


@dp.message(UploadStates.AUDIO_UPLOAD, F.audio)
async def handle_audio(message: Message, state: FSMContext, bot: Bot):
    try:
        file = await bot.get_file(message.audio.file_id)
        path = Path("temp") / f"{message.from_user.id}_audio.mp3"
        await bot.download_file(file.file_path, path)

        # Проверка длительности аудио
        audio = AudioFileClip(str(path))
        if audio.duration > 600:  # 10 минут максимум
            await message.answer("❌ Аудио должно быть короче 10 минут!")
            return

        await state.update_data(audio_path=str(path))
        await message.answer("⏳ Создаю видео...")
        await state.set_state(UploadStates.VIDEO_GENERATION)
        await generate_video(message.from_user.id, state)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

@dp.callback_query(UploadStates.CONTENT_TYPE, F.data.in_(["ready_video", "photo_audio"]))
async def handle_content_type(callback: CallbackQuery, state: FSMContext):
    if callback.data == "ready_video":
        await callback.message.answer("📤 Отправьте видео:")
        await state.set_state(UploadStates.MEDIA_UPLOAD)
    else:
        await callback.message.answer("📤 Отправьте фото:")
        await state.set_state(UploadStates.PHOTO_UPLOAD)
    await callback.answer()

@dp.callback_query(UploadStates.CONTENT_TYPE, F.data == "multi_channel")
async def handle_multi_channel(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Сколько каналов вы хотите использовать? (1-10)")
    await state.set_state(UploadStates.MULTI_CHANNEL_COUNT)
    await callback.answer()

@dp.message(Command("upload"))
async def cmd_upload(message: types.Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Готовое видео", callback_data="ready_video"),
            InlineKeyboardButton(text="Фото + MP3", callback_data="photo_audio")
        ],
        [
            InlineKeyboardButton(text="Мультиканальная загрузка", callback_data="multi_channel")
        ]
    ])
    await message.answer("📤 Выберите тип контента:", reply_markup=keyboard)
    await state.set_state(UploadStates.CONTENT_TYPE)
@dp.message(UploadStates.MEDIA_UPLOAD, F.video)
async def handle_video_upload(message: types.Message, state: FSMContext):
    try:
        # Скачивание видео
        video = message.video
        file = await bot.get_file(video.file_id)
        path = Path("temp") / f"{message.from_user.id}_video.mp4"
        await bot.download_file(file.file_path, path)

        # Загрузка на YouTube
        await message.answer("⏳ Видео загружается на YouTube...")
        video_id = await upload_video(
            user_id=message.from_user.id,
            video_path=str(path),
            title="Мое видео",
            description="Загружено через бота"
        )

        await message.answer(f"✅ Видео успешно загружено! ID: {video_id}")
        await state.clear()

    except Exception as e:
        logger.error(f"Ошибка загрузки видео: {str(e)}", exc_info=True)
        await message.answer("❌ Не удалось загрузить видео.")
    finally:
        if path.exists():
            path.unlink()

@dp.message(Command("vpn"))
async def cmd_vpn(message: Message, state: FSMContext):
    await message.answer("🔐 Отправьте конфиг VPN в формате .ovpn:")
    await state.set_state(UploadStates.VPN_CONFIG)

async def save_vpn_config(user_id: int, channel_id: str, config: str):
    encrypted = fernet.encrypt(config.encode())
    await storage.redis.hset(
        f"user:{user_id}",
        f"vpn:{channel_id}",
        encrypted.decode()
    )


@dp.message(UploadStates.VPN_CONFIG, F.document)
async def handle_vpn_config(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        channel_id = data["current_channel_id"]

        # Скачивание и сохранение конфига
        file = await bot.get_file(message.document.file_id)
        path = Path("temp") / f"{message.from_user.id}_vpn.ovpn"
        await bot.download_file(file.file_path, path)

        with open(path, "r") as f:
            config = f.read()

        await save_vpn_config(message.from_user.id, channel_id, config)
        await ask_for_vpn_config(message, state)  # Запрашиваем следующий конфиг

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
    finally:
        path.unlink(missing_ok=True)

# Обработчик выбора количества каналов
@dp.callback_query(UploadStates.MULTI_CHANNEL)
async def handle_multi_channel(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите количество каналов (1-10):")
    await state.set_state(UploadStates.MULTI_CHANNEL_COUNT)


# Обработчик настройки каналов
@dp.message(UploadStates.MULTI_CHANNEL_COUNT)
async def handle_channel_setup(message: Message, state: FSMContext):
    try:
        count = int(message.text)
        if not 1 <= count <= 10:
            raise ValueError

        await state.update_data(channel_count=count, current_channel=1)
        await message.answer(f"Настройка канала 1/{count}\nВведите название канала:")
        await state.set_state(UploadStates.MULTI_CHANNEL_SETUP)

    except:
        await message.answer("❌ Введите число от 1 до 10!")


# Циклическая настройка каналов
@dp.message(UploadStates.MULTI_CHANNEL_SETUP)
async def handle_channel_config(message: Message, state: FSMContext):
    data = await state.get_data()
    current = data["current_channel"]
    total = data["channel_count"]

    # Сохраняем название канала
    await storage.redis.hset(
        f"user:{message.from_user.id}:channels",
        f"channel_{current}",
        message.text
    )

    if current < total:
        await state.update_data(current_channel=current + 1)
        await message.answer(
            f"Настройка канала {current + 1}/{total}\nВведите название канала:"
        )
    else:
        await message.answer("✅ Все каналы настроены! Теперь загрузите VPN-конфиги.")
        await state.set_state(UploadStates.VPN_CONFIG)


async def upload_to_multiple_channels(user_id: int, video_path: str):
    channels = await storage.redis.hgetall(f"user:{user_id}:channels")
    for channel_num, channel_name in channels.items():
        channel_id = channel_num.decode().split("_")[1]

        # Подключение VPN
        vpn_config = await get_vpn_for_channel(user_id, channel_id)
        connect_to_vpn(vpn_config)

        # Загрузка видео
        await upload_to_youtube(user_id, video_path, channel_id)

        await bot.send_message(
            user_id,
            f"✅ Видео загружено на канал: {channel_name.decode()}"
        )

async def get_youtube_channels(user_id: int) -> list:
    """Получает список каналов пользователя через YouTube API"""
    credentials = await get_valid_credentials(user_id)
    if not credentials:
        return []

    youtube = build("youtube", "v3", credentials=credentials)
    request = youtube.channels().list(
        part="snippet",
        mine=True
    )
    response = request.execute()
    return [
        (item["id"], item["snippet"]["title"])
        for item in response.get("items", [])
    ]



@dp.message(UploadStates.CHANNEL_SELECT)
async def handle_channel_select(message: Message, state: FSMContext):
    try:
        channels = await get_user_channels(message.from_user.id)
        if not channels:
            await message.answer("❌ Каналы не настроены! Используйте /setup_channels")
            return

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=name, callback_data=id)]
                             for id, name in channels])
        await message.answer("📡 Выберите канал:", reply_markup=keyboard)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


async def upload_to_youtube(user_id: int, video_path: str, channel_id: str):
    try:
        credentials = await get_valid_credentials(user_id)
        if not credentials:
            raise ValueError("❌ Токен не найден")

        youtube = build("youtube", "v3", credentials=credentials)
        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": f"Video for {channel_id}",
                    "description": "Загружено через бота",
                    "categoryId": "22"
                },
                "status": {"privacyStatus": "private"}
            },
            media_body=MediaFileUpload(video_path)
        )
        response = request.execute()
        return response["id"]
    except Exception as e:
        logger.error(f"Ошибка загрузки: {str(e)}")

@dp.callback_query(UploadStates.CHANNEL_SELECT)
async def handle_channel_upload(callback: CallbackQuery, state: FSMContext):
    channel_id = callback.data
    data = await state.get_data()

    # Подключение к VPN
    vpn_config = await get_vpn_for_channel(callback.from_user.id, channel_id)
    connect_to_vpn(vpn_config)

    # Загрузка видео
    await upload_to_youtube(
        user_id=callback.from_user.id,
        video_path=data["video_path"],
        channel_id=channel_id
    )
    await callback.message.answer("✅ Видео загружено!")
    await state.clear()

@dp.message(UploadStates.OAUTH_FLOW)
async def handle_oauth_code(message: types.Message, state: FSMContext):
    try:
        code = message.text.strip()
        data = await state.get_data()

        logger.info(f"Получен код: {code}")
        logger.debug(f"Данные состояния: {data}")

        logger.debug(f"client_config: {data['client_config']}")
        logger.debug(f"scopes: {data['scopes']}")
        logger.debug(f"redirect_uri: {data['redirect_uri']}")

        # Проверка наличия всех необходимых данных
        if not all(key in data for key in ["client_config", "scopes", "redirect_uri"]):
            await message.answer("❌ Сначала отправьте client_secrets.json!")
            return

        # Создание OAuth-потока с явным указанием client_config
        flow = InstalledAppFlow.from_client_config(
            client_config={"installed": data["client_config"]},
            scopes=data["scopes"],
            redirect_uri=data["redirect_uri"]
        )

        # Получение токена
        flow.fetch_token(code=code)
        credentials = flow.credentials

        # Сохранение токена
        token_data = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "expiry": credentials.expiry.isoformat(),
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "token_uri": credentials.token_uri,
            "scopes": credentials.scopes
        }
        encrypted = fernet.encrypt(json.dumps(token_data).encode())
        await update_user_data(message.from_user.id, {"youtube_token": encrypted.decode()})

        await message.answer("✅ Авторизация успешно завершена! Теперь вы можете использовать /upload.")
        await state.clear()

    except Exception as e:
        logger.error(f"Ошибка: {str(e)}", exc_info=True)
        await message.answer("❌ Ошибка авторизации. Проверьте код и попробуйте снова.")

@dp.message(Command("guide"))
async def cmd_guide(message: types.Message):
    instructions = (
        "📚 **Новая инструкция:**\n"
        "1. /auth - Авторизация в YouTube\n"
        "2. /setup_channels - Получить ваши каналы\n"
        "3. /setup_vpn - Настроить VPN для каналов\n"
        "4. /upload - Начать загрузку\n"
    )
    await message.answer(instructions, parse_mode="Markdown")

@dp.message(Command("view_configs"))
async def cmd_view_configs(message: types.Message):
    try:
        user_data = await get_user_data(message.from_user.id)
        configs = []

        for key in user_data:
            if key.startswith(("vpn:", "proxy", "youtube_token")):
                configs.append(f"🔑 {key}")

        if configs:
            await message.answer(
                "📂 <b>Сохраненные конфигурации:</b>\n" + "\n".join(configs),
                parse_mode="HTML"
            )
        else:
            await message.answer("❌ Нет сохраненных конфигураций!")

    except Exception as e:
        logger.error(f"Ошибка /view_configs: {str(e)}")
        await message.answer("⚠️ Ошибка при получении данных.")


@dp.message(Command("delete_config"))
async def cmd_delete_config(message: types.Message):
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("❌ Укажите ключ конфигурации!")
            return

        config_key = args[1].strip()
        user_data = await get_user_data(message.from_user.id)

        if config_key not in user_data:
            await message.answer(f"❌ Конфигурация '{config_key}' не найдена!")
            return

        await storage.redis.hdel(f"user:{message.from_user.id}", config_key)
        await message.answer(f"✅ Конфигурация '{config_key}' удалена!")

    except Exception as e:
        logger.error(f"Ошибка /delete_config: {str(e)}")
        await message.answer("⚠️ Ошибка при удалении.")


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
        logger.error(f"Ошибка учетных данных: {str(e)}")
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
        logger.critical(f"Критическая ошибка: {str(e)}", exc_info=True)
    finally:
        await release_lock()
        logger.info("Ресурсы освобождены")


if __name__ == "__main__":
    asyncio.run(main())