import os
import sys
import logging
import tempfile
import subprocess
import asyncio
import signal
import json
from datetime import datetime, timezone, UTC
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta, timezone
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
from aiogram.fsm.storage.base import StorageKey  # Добавить в импорты
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils import keyboard
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
    METADATA_INPUT = State()       # Ввод метаданных видео
    VPN_CHOICE = State()           # Выбор необходимости VPN
    VPN_CONFIG_UPLOAD = State()    # Загрузка VPN-конфига


# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================

# ================== ЗАГРУЗКА ВИДЕО ==================
async def upload_video(user_id: int, video_path: str, metadata: dict) -> str:
    try:
        # Получение токена
        credentials = await get_valid_credentials(user_id)
        if not credentials:
            raise ValueError("❌ Токен не найден. Выполните /auth.")

        # Создание клиента YouTube
        youtube = build("youtube", "v3", credentials=credentials)

        if len(metadata['tags']) > 10:
            raise ValueError("Максимум 10 тегов")
        if len(metadata['title']) > 100:
            raise ValueError("Слишком длинное название")

        # Загрузка видео
        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": metadata['title'],  # Используем metadata из параметров
                    "description": metadata['description'],
                    "tags": metadata['tags'],
                    "categoryId": "10"
                },
                "status": {
                    "privacyStatus": "private",
                    "publishAt": metadata['publish_time'] if metadata['is_scheduled'] else None,
                    "selfDeclaredMadeForKids": False
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

        if not await storage.redis.ping():
            raise ConnectionError("Redis недоступен")

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
            expiry_time = datetime.fromisoformat(credentials.expiry).replace(tzinfo=timezone.utc)
            time_left = expiry_time - datetime.now(timezone.utc)

            if time_left.total_seconds() > 0:
                token_status = (
                    f"\n\n🔐 Статус авторизации: Действителен еще "
                    f"{time_left // timedelta(hours=1)} ч. "
                    f"{(time_left % timedelta(hours=1)) // timedelta(minutes=1)} мин."
                )
            else:
                token_status = "\n\n⚠️ Токен истек! Используйте /auth"

        commands = (
            "🎥 *Доступные команды:*\n"
            "• `/start` — Начальное меню\n"
            "• `/auth` — Авторизация в YouTube\n"
            "• `/upload` — Загрузить видео\n"
            "• `/view_configs` — Показать конфиги\n"
            "• `/delete_config` — Удалить конфиг\n"
            "• `/guide` — Инструкция\n"
            "• `/setup_channels` — Настроить каналы\n"
            "• `/cancel` — Отменить текущую операцию\n"
        )
        await message.answer(commands, parse_mode="MarkdownV2")


    except Exception as e:
        logger.error(f"Ошибка /start: {str(e)}")
        await message.answer("⚠️ Ошибка подключения к Redis. Проверьте сервер.")

@dp.callback_query(UploadStates.VPN_CHOICE, F.data.in_(["use_vpn", "no_vpn"]))
async def handle_vpn_choice(callback: CallbackQuery, state: FSMContext):
    if callback.data == "use_vpn":
        await callback.message.answer(
            "📤 Отправьте файл VPN-конфига (.ovpn) и укажите его название в формате:\nНазвание_конфига")
        await state.set_state(UploadStates.VPN_CONFIG_UPLOAD)
    else:
        await state.update_data(vpn_config=None)
        await handle_channel_select(callback.message, state)
    await callback.answer()


@dp.message(UploadStates.VPN_CONFIG_UPLOAD)
async def handle_vpn_config_upload(message: Message, state: FSMContext):
    try:
        # Проверяем наличие документа и подписи
        if not message.document:
            await message.answer("❌ Вы не отправили файл!")
            return

        if not message.caption:
            await message.answer("❌ Укажите название конфига в подписи к файлу!")
            return

        # Извлекаем название из первой строки подписи
        config_name = message.caption.strip().split('\n')[0].strip()
        if not config_name:
            await message.answer("❌ Название конфига не может быть пустым!")
            return

        # Скачиваем файл
        file = await bot.get_file(message.document.file_id)
        path = Path("temp") / f"{message.from_user.id}_vpn.ovpn"
        await bot.download_file(file.file_path, path)

        # Читаем содержимое
        with open(path, 'r') as f:
            config_data = f.read()

        # Проверяем валидность конфига
        if "client" not in config_data:
            await message.answer("❌ Это не валидный конфиг OpenVPN!")
            path.unlink(missing_ok=True)
            return

        # Сохраняем данные
        await state.update_data(vpn_config={
            'name': config_name,
            'data': config_data
        })

        # Удаляем временный файл
        path.unlink(missing_ok=True)

        await message.answer(f"✅ Конфиг '{config_name}' успешно сохранен!")
        await handle_channel_select(message, state)

    except Exception as e:
        logger.error(f"Ошибка загрузки VPN: {str(e)}", exc_info=True)
        await message.answer("❌ Произошла ошибка при обработке конфига!")

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
    try:
        if not config.startswith("client"):
            raise ValueError("Неверный формат конфига OpenVPN")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ovpn") as f:
            f.write(config.encode())
            f.flush()  # Важно для записи на диск

        # Добавляем таймаут 15 секунд
        result = subprocess.run(
            ["sudo", "openvpn", "--config", f.name],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            text=True
        )
        logger.info("VPN подключён успешно")
        return True
    except subprocess.CalledProcessError as e:
        error_msg = f"Ошибка подключения: {e.stderr.strip()}"
        if "AUTH_FAILED" in error_msg:
            error_msg += "\n🔑 Неверные учетные данные VPN"
        logger.error(error_msg)
        raise ValueError(error_msg)
    except subprocess.TimeoutExpired:
        error_msg = "Таймаут подключения к VPN"
        logger.error(error_msg)
        raise ValueError(error_msg)

    finally:
        if 'f' in locals():
            os.unlink(f.name)  # Удаляем временный файл

async def save_channel(user_id: int, channel_name: str, channel_id: str):
    await storage.redis.hset(
        f"user:{user_id}:channels",
        channel_id,  # Ключ - channel_id
        channel_name  # Значение - название канала
    )

async def get_user_channels(user_id: int) -> list:
    channels = await storage.redis.hgetall(f"user:{user_id}:channels")
    return [(k.decode(), v.decode()) for k, v in channels.items()]


@dp.message(Command("setup_channels"))
async def cmd_setup_channels(message: Message, state: FSMContext):
    try:
        channels = await get_youtube_channels(message.from_user.id)
        if not channels:
            await message.answer(
                "❌ Нет доступных каналов. Убедитесь что:\n1. Канал привязан к аккаунту\n2. Вы дали разрешение 'Управление YouTube'")
            return

        await storage.redis.delete(f"user:{message.from_user.id}:channels")
        await storage.redis.hset(
            f"user:{message.from_user.id}:channels",
            mapping={channel_id: name for channel_id, name in channels}
        )
        await message.answer(f"✅ Найдено каналов: {len(channels)}\nИспользуйте /upload")

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

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
            scopes=["https://www.googleapis.com/auth/youtube"],  # Упростили до одного scope
            redirect_uri="urn:ietf:wg:oauth:2.0:oob"
        )
        auth_url, _ = flow.authorization_url(prompt="consent")  # Определяем auth_url здесь

        await state.update_data(
            client_config=data["installed"],  # Важно!
            scopes=["https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/youtube.upload"],
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
            "📝 Введите метаданные в формате:\n"
            "Название\nОписание\nТеги (через запятую)\nДата публикации (YYYY-MM-DDTHH:MM:SSZ или 'сейчас')\n\n"
            "Пример:\nМое видео\nОписание\nтег1,тег2\nсейчас"
        )
        await state.set_state(UploadStates.METADATA_INPUT)

    except Exception as e:
        await bot.send_message(user_id, f"❌ Ошибка: {str(e)}")

@dp.message(Command("channel_select"))
async def cmd_channel_select(message: Message, state: FSMContext):
    await handle_channel_select(message, state)

@dp.message(UploadStates.PHOTO_UPLOAD, F.photo)
async def handle_photo(message: Message, state: FSMContext, bot: Bot):
    file = await bot.get_file(message.photo[-1].file_id)
    path = Path("temp") / f"{message.from_user.id}_photo.jpg"
    await bot.download_file(file.file_path, path)
    await state.update_data(photo_path=str(path))
    await message.answer("🎵 Теперь отправьте MP3-аудио:")
    await state.set_state(UploadStates.AUDIO_UPLOAD)


@dp.message(UploadStates.METADATA_INPUT)
async def handle_metadata(message: Message, state: FSMContext):
    from datetime import datetime
    from dateutil.parser import parse
    try:
        parts = message.text.split('\n')
        if len(parts) != 4:
            raise ValueError("Неверный формат данных")

        title = parts[0].strip()
        description = parts[1].strip()
        tags = [tag.strip() for tag in parts[2].split(',')]
        publish_time = parts[3].strip().lower()

        # Парсинг даты
        if publish_time != 'сейчас':
            from dateutil.parser import parse
            parse(publish_time)
            is_scheduled = True
        else:
            from datetime import UTC
            publish_time = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            is_scheduled = False

        await state.update_data(
            video_metadata={
                'title': title,
                'description': description,
                'tags': tags,
                'publish_time': publish_time,
                'is_scheduled': is_scheduled
            }
        )

        # Запрос о необходимости VPN
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="use_vpn"),
             InlineKeyboardButton(text="Нет", callback_data="no_vpn")]
        ])
        await message.answer("🔐 Использовать VPN/прокси для загрузки?", reply_markup=keyboard)
        await state.set_state(UploadStates.VPN_CHOICE)

    except Exception as e:
        logger.error(f"Ошибка метаданных: {str(e)}")
        await message.answer("❌ Ошибка формата! Повторите ввод:")

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
    await callback.answer()

@dp.message(Command("upload"))
async def cmd_upload(message: types.Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Готовое видео", callback_data="ready_video"),
                InlineKeyboardButton(text="Фото + MP3", callback_data="photo_audio")
            ],
            [
                InlineKeyboardButton(text="Мультиканальная загрузка", callback_data="multi_channel")
            ]
        ]
    )
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

async def upload_to_multiple_channels(user_id: int, video_path: str, state: FSMContext):
    data = await state.get_data()
    channels = await storage.redis.hgetall(f"user:{user_id}:channels")
    for channel_num, channel_name in channels.items():
        channel_id = channel_num.decode().split("_")[1]

        # Подключение VPN
        vpn_config = await get_vpn_for_channel(user_id, channel_id)
        connect_to_vpn(vpn_config)

        # Загрузка видео
        await upload_to_youtube(
            user_id=user_id,
            video_path=video_path,
            channel_id=channel_id,
            metadata=data['video_metadata']  # Передаем метаданные
        )

        await bot.send_message(
            user_id,
            f"✅ Видео загружено на канал: {channel_name.decode()}"
        )
    for channel_id, channel_name in channels:
        # Применение VPN если есть
        if 'vpn_config' in data:
            connect_to_vpn(data['vpn_config']['data'])

        await upload_to_youtube(
            user_id=user_id,
            video_path=video_path,
            channel_id=channel_id,
            metadata=data['video_metadata']
        )


async def get_youtube_channels(user_id: int) -> list:
    try:
        credentials = await get_valid_credentials(user_id)
        if not credentials:
            return []

        youtube = build("youtube", "v3", credentials=credentials)
        request = youtube.channels().list(
            part="snippet",
            mine=True,
            managedByMe=True  # Добавили фильтр для каналов пользователя
        )
        response = request.execute()
        return [
            (item["id"], item["snippet"]["title"])
            for item in response.get("items", [])
        ]
    except Exception as e:
        logger.error(f"Ошибка получения каналов: {str(e)}")
        return []


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
        await state.set_state(UploadStates.CHANNEL_SELECT)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


async def upload_to_youtube(user_id: int, video_path: str, channel_id: str, metadata: dict):
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

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("♻️ Все операции отменены.")

@dp.callback_query(UploadStates.CHANNEL_SELECT)
async def handle_channel_selection(callback: CallbackQuery, state: FSMContext):
    try:
        channel_id = callback.data
        # Получаем каналы из Redis, а не из Google API
        channels = await get_user_channels(callback.from_user.id)
        channel_name = next((name for id, name in channels if id == channel_id), "Неизвестный канал")

        if channel_name == "Неизвестный канал":
            await callback.message.answer("❌ Канал не найден!")
            return

        await state.update_data(selected_channel=channel_id)
        await callback.message.edit_text(f"✅ Выбран канал: {channel_name}")
        await show_content_type_menu(callback.message, state)
    except Exception as e:
        logger.error(f"Ошибка выбора канала: {str(e)}")


async def show_content_type_menu(message: Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Готовое видео", callback_data="ready_video"),
                InlineKeyboardButton(text="Фото + MP3", callback_data="photo_audio")
            ],
            [
                InlineKeyboardButton(text="Мультиканальная загрузка", callback_data="multi_channel")
            ]
        ]
    )
    await message.answer("📤 Выберите тип контента:", reply_markup=keyboard)
    await state.set_state(UploadStates.CONTENT_TYPE)

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

        channels = await get_youtube_channels(message.from_user.id)
        if not channels:
            await message.answer("❌ Нет доступных каналов.")
            return

        if len(channels) == 1:
            await state.update_data(selected_channel=channels[0][0])
            await message.answer(f"🎯 Автоматически выбран канал: {channels[0][1]}")
            await show_content_type_menu(message)
        else:
            await show_channel_selection(message, channels, state)

        await message.answer("📡 Выберите канал для загрузки:", reply_markup=keyboard)
        await state.set_state(UploadStates.CHANNEL_SELECT)

    except Exception as e:
        logger.error(f"Ошибка: {str(e)}", exc_info=True)
        await message.answer("❌ Ошибка авторизации. Проверьте код и попробуйте снова.")


async def show_channel_selection(message: Message, channels: list, state: FSMContext):
    try:
        if not channels:
            await message.answer("❌ Нет доступных каналов")
            return

        buttons = []
        for channel_id, channel_name in channels:
            # Исправлен порядок аргументов
            buttons.append(
                [InlineKeyboardButton(text=channel_name, callback_data=channel_id)]
            )

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer("📡 Выберите канал:", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}")

@dp.message(Command("refresh_channels"))
async def cmd_refresh_channels(message: Message, state: FSMContext):
    await state.clear()
    try:
        channels = await get_youtube_channels(message.from_user.id)
        if channels:
            await show_channel_selection(message, channels, state)
        else:
            await message.answer("❌ Нет доступных каналов.")
    except Exception as e:
        await message.answer("⚠️ Ошибка обновления списка каналов")

async def reset_state_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("♻️ Предыдущая операция отменена")

# Регистрируем для основных команд
dp.message.register(reset_state_handler, Command(commands=["start", "auth", "upload", "refresh_channels"]))

@dp.message(Command("guide"))
async def cmd_guide(message: types.Message):
    instructions = (
        "📚 *Инструкция:*\n"
        "1. `/auth` — Авторизация в YouTube\n"
        "2. `/setup_channels` — Получить список каналов\n"
        "3. `/upload` — Загрузить видео\n"
        "4. `/view_configs` — Показать настройки\n"
        "5. `/delete_config` — Удалить конфиг\n\n"
        "⚠️ *Перед загрузкой выполните* `/auth`"
    )
    await message.answer(instructions, parse_mode="MarkdownV2")

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
        # Получаем список конфигураций
        user_data = await get_user_data(message.from_user.id)
        configs = [key for key in user_data if key.startswith(("vpn:", "youtube_token"))]

        if not configs:
            await message.answer("❌ Нет сохраненных конфигураций!")
            return

        # Создаем инлайн-клавиатуру с конфигурациями
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=key, callback_data=f"delete_{key}")]  # Закрывающая скобка для кнопки
                for key in configs  # Добавлен пробел для читаемости
            ]
        )
        await message.answer("🗑️ Выберите конфигурацию для удаления:", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Ошибка /delete_config: {str(e)}")
        await message.answer("⚠️ Ошибка при получении данных.")

@dp.callback_query(F.data.startswith("delete_"))
async def handle_delete_config(callback: CallbackQuery):
    config_key = callback.data.split("_", 1)[1]
    try:
        await storage.redis.hdel(f"user:{callback.from_user.id}", config_key)
        await callback.message.answer(f"✅ Конфигурация '{config_key}' удалена!")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {str(e)}")
    await callback.answer()

# ================== ЗАГРУЗКА ВИДЕО ==================
async def get_valid_credentials(user_id: int) -> Optional[Credentials]:
    try:
        encrypted = await decrypt_user_data(user_id, "youtube_token")
        if not encrypted:
            return None

        token_data = json.loads(encrypted.decode())

        # Преобразование строки expiry в datetime
        if isinstance(token_data["expiry"], str):
            token_data["expiry"] = datetime.fromisoformat(token_data["expiry"])

        # Проверка срока действия
        if datetime.now(timezone.utc) > token_data["expiry"] - timedelta(minutes=5):
            credentials = Credentials(**token_data)
            credentials.refresh(Request())

            # Обновляем данные токена
            token_data.update({
                "token": credentials.token,
                "expiry": credentials.expiry.isoformat()  # Сохраняем как строку
            })

            # Шифруем и сохраняем
            encrypted = fernet.encrypt(json.dumps(token_data).encode())
            await update_user_data(user_id, {"youtube_token": encrypted.decode()})

        return Credentials(**token_data)

    except Exception as e:
        logger.error(f"Ошибка в get_valid_credentials: {str(e)}", exc_info=True)
        return None

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("♻️ Все операции отменены.")

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