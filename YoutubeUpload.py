import os
import sys
import logging
import tempfile
import subprocess
import asyncio
import signal
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
from cryptography.fernet import Fernet

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

# Проверка обязательных переменных
REQUIRED_ENV = ["TELEGRAM_TOKEN", "REDIS_URL", "ENCRYPTION_KEY"]
if missing := [var for var in REQUIRED_ENV if not os.getenv(var)]:
    raise EnvironmentError(f"Missing environment variables: {missing}")

# Инициализация компонентов
bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
storage = RedisStorage.from_url(os.getenv("REDIS_URL"))
dp = Dispatcher(storage=storage)
fernet = Fernet(os.getenv("ENCRYPTION_KEY").encode())


class UploadStates(StatesGroup):
    CONTENT_TYPE = State()
    MEDIA_UPLOAD = State()
    METADATA = State()
    VPN_CONFIG = State()
    PROXY = State()
    YOUTUBE_TOKEN = State()


LOCK_KEY = "bot_lock"
LOCK_TTL = 60


def validate_metadata(metadata: Dict) -> bool:
    required_fields = ['title', 'description', 'tags']
    return all(field in metadata for field in required_fields)


async def get_user_data(user_id: int) -> Dict:
    data = await storage.redis.hgetall(f"user:{user_id}")
    return {k.decode(): v.decode() for k, v in data.items()}


async def update_user_data(user_id: int, data: Dict) -> None:
    await storage.redis.hset(f"user:{user_id}", mapping=data)


async def acquire_lock() -> bool:
    try:
        return await storage.redis.set(LOCK_KEY, "locked", nx=True, ex=LOCK_TTL)
    except Exception as e:
        logger.error(f"Redis error: {e}")
        return False


async def release_lock():
    try:
        await storage.redis.delete(LOCK_KEY)
    except Exception as e:
        logger.error(f"Failed to release lock: {e}")


async def shutdown(signal, loop):
    logger.info(f"Received exit signal {signal.name}...")
    await release_lock()
    await bot.close()
    await storage.close()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🎥 *YouTube Upload Bot*\n\n"
        "📚 **Инструкция:**\n"
        "1. Используйте /upload для начала загрузки\n"
        "2. Выберите тип контента:\n"
        "   - 🎥 Видео: отправьте MP4-файл\n"
        "   - 🖼️ Аудио+Изображение: отправьте MP3 и фото\n"
        "3. Введите метаданные в формате:\n"
        "   <b>Название</b>\n"
        "   <b>Описание</b>\n"
        "   <b>Теги</b> (через запятую)\n"
        "   <b>Дата публикации</b>\n\n"
        "⚙️ Настройка VPN/прокси доступна на этапе загрузки.",
        parse_mode="HTML"
    )


@dp.message(Command("upload"))
async def cmd_upload(message: types.Message, state: FSMContext):
    await state.set_state(UploadStates.CONTENT_TYPE)
    await message.answer(
        "📁 Выберите тип контента:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🎥 Видео", callback_data="video")],
            [types.InlineKeyboardButton(text="🖼️ Аудио+Изображение", callback_data="audio_image")]
        ])
    )


@dp.callback_query(F.data.in_(["video", "audio_image"]))
async def content_type_handler(callback: types.CallbackQuery, state: FSMContext):
    try:
        await state.update_data(content_type=callback.data)
        await callback.message.answer(
            "📤 Отправьте видео файл (MP4)" if callback.data == "video"
            else "🎵 Отправьте аудио файл (MP3)"
        )
        await state.set_state(UploadStates.MEDIA_UPLOAD)
        await callback.answer()
    except TelegramBadRequest as e:
        logger.warning(f"Пропущен запрос: {e}")


@dp.message(UploadStates.MEDIA_UPLOAD, F.audio)
async def audio_handler(message: types.Message, state: FSMContext, bot: Bot):
    try:
        file = await bot.get_file(message.audio.file_id)
        path = Path("temp") / f"{message.from_user.id}_audio.mp3"
        await bot.download_file(file.file_path, path)
        await state.update_data(audio_path=str(path))
        await message.answer("📸 Теперь отправьте изображение (JPG/PNG)")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


@dp.message(UploadStates.MEDIA_UPLOAD, F.photo)
async def image_handler(message: types.Message, state: FSMContext, bot: Bot):
    try:
        file = await bot.get_file(message.photo[-1].file_id)
        path = Path("temp") / f"{message.from_user.id}_image.jpg"
        await bot.download_file(file.file_path, path)
        await state.update_data(image_path=str(path))

        data = await state.get_data()
        if 'audio_path' in data and 'image_path' in data:
            await state.set_state(UploadStates.METADATA)
            await message.answer("📝 Введите метаданные...")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


@dp.message(UploadStates.METADATA)
async def metadata_handler(message: types.Message, state: FSMContext):
    try:
        parts = message.text.split('\n')
        if len(parts) < 3:
            raise ValueError("Недостаточно данных")

        metadata = {
            'title': parts[0],
            'description': parts[1],
            'tags': parts[2].split(','),
            'publish_at': parts[3] if len(parts) > 3 else 'сейчас'
        }

        if not validate_metadata(metadata):
            raise ValueError("Неверный формат метаданных")

        await state.update_data(metadata=metadata)
        await message.answer(
            "⚙️ Хотите настроить VPN/Прокси?",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🛡️ Да", callback_data="setup_network")],
                [types.InlineKeyboardButton(text="🚀 Пропустить", callback_data="skip_network")]
            ])
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}\nПопробуйте еще раз")


@dp.callback_query(F.data == "setup_network")
async def setup_network_handler(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🔒 Выберите тип настройки:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🛡️ VPN", callback_data="setup_vpn")],
            [types.InlineKeyboardButton(text="🔗 Прокси", callback_data="setup_proxy")]
        ])
    )
    await callback.answer()


@dp.callback_query(F.data == "setup_vpn")
async def setup_vpn_handler(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📎 Отправьте конфиг VPN в формате:\n"
        "1. Укажите тип VPN (OpenVPN/WireGuard) в заголовке\n"
        "2. Прикрепите файл конфигурации\n"
        "Пример: <code>OpenVPN; MyVPN</code>"
    )
    await state.set_state(UploadStates.VPN_CONFIG)
    await callback.answer()


@dp.message(UploadStates.VPN_CONFIG, F.document)
async def vpn_config_handler(message: types.Message, state: FSMContext, bot: Bot):
    try:
        if not message.caption or ";" not in message.caption:
            await message.answer(
                "❌ Неверный формат заголовка!\n"
                "📝 Пример правильного формата:\n"
                "<code>OpenVPN; MyVPN</code>\n\n"
                "Отправьте файл конфигурации снова с правильным заголовком."
            )
            return

        vpn_type, name = message.caption.split(";", 1)
        vpn_type = vpn_type.strip()
        name = name.strip()

        file = await bot.get_file(message.document.file_id)
        path = Path("temp") / f"{message.from_user.id}_vpn.conf"

        await bot.download_file(file.file_path, path)
        with open(path, "rb") as config_file:
            result = await save_encrypted_file(message.from_user.id, config_file.read(), f"vpn:{name}")

        await message.answer(f"✅ {result}\n\nПродолжаем загрузку...")
        path.unlink()
        await start_upload_process(message, state)

    except Exception as e:
        logger.error(f"Ошибка VPN: {str(e)}")
        await message.answer(
            f"❌ Ошибка: {str(e)}\n"
            "Проверьте формат файла и попробуйте снова."
        )


async def save_encrypted_file(user_id: int, file_bytes: bytes, prefix: str) -> str:
    encrypted = fernet.encrypt(file_bytes)
    await update_user_data(user_id, {prefix: encrypted.decode()})
    return "Успешно сохранено"


@dp.message(UploadStates.PROXY)
async def proxy_handler(message: types.Message, state: FSMContext):
    try:
        if not any(message.text.startswith(proto) for proto in ("http://", "https://", "socks5://")):
            raise ValueError("Неверный формат прокси")

        await save_encrypted_file(message.from_user.id, message.text.encode(), "proxy")
        await message.answer("✅ Прокси сохранен!")
        await start_upload_process(message, state)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


async def decrypt_user_data(user_id: int, key: str) -> Optional[bytes]:
    user_data = await get_user_data(user_id)
    if encrypted := user_data.get(key):
        return fernet.decrypt(encrypted.encode())
    return None


async def start_upload_process(message: types.Message, state: FSMContext):
    state_data = await state.get_data()
    try:
        user_data = await get_user_data(message.from_user.id)

        if proxy_data := await decrypt_user_data(message.from_user.id, "proxy"):
            proxy = proxy_data.decode()
            os.environ.update({'HTTP_PROXY': proxy, 'HTTPS_PROXY': proxy})

        if not (token_data := await decrypt_user_data(message.from_user.id, "youtube_token")):
            await message.answer("🔑 Отправьте токен YouTube API (файл .json)")
            await state.set_state(UploadStates.YOUTUBE_TOKEN)
            return

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp.write(token_data)
            token_path = tmp.name

        vpn_connected = False
        for key in user_data:
            if key.startswith("vpn:"):
                try:
                    vpn_data = await decrypt_user_data(message.from_user.id, key)
                    with tempfile.NamedTemporaryFile(delete=False) as vpn_file:
                        vpn_file.write(vpn_data)
                        vpn_path = vpn_file.name

                    vpn_type = key.split(":")[1]
                    cmd = ["openvpn", "--config", vpn_path] if vpn_type == "openvpn" \
                        else ["wg-quick", "up", vpn_path]

                    subprocess.run(cmd, check=True, timeout=30)
                    vpn_connected = True
                    break
                except Exception as e:
                    logger.error(f"Ошибка VPN: {e}")
                finally:
                    Path(vpn_path).unlink(missing_ok=True)

        if state_data.get('content_type') == 'audio_image':
            video_path = await create_video_from_media(
                state_data['image_path'],
                state_data['audio_path']
            )
        else:
            video_path = state_data.get('video_path')

        service = await asyncio.to_thread(build_youtube_service, token_path)
        video_id = await asyncio.to_thread(upload_video, service, video_path, state_data['metadata'])
        await message.answer(f"✅ Видео успешно загружено! ID: {video_id}")

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.exception("Ошибка загрузки")
    finally:
        for file in ['video_path', 'audio_path', 'image_path']:
            if path := state_data.get(file):
                Path(path).unlink(missing_ok=True)
        await state.clear()


def build_youtube_service(token_path: str):
    flow = InstalledAppFlow.from_client_secrets_file(
        token_path,
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    credentials = flow.run_local_server(port=8080)
    os.unlink(token_path)
    return build("youtube", "v3", credentials=credentials)


def upload_video(service, video_path: str, metadata: Dict) -> str:
    request_body = {
        "snippet": {
            "title": metadata['title'],
            "description": metadata['description'],
            "tags": metadata['tags'],
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": "public",
            "publishAt": metadata.get('publish_at'),
            "selfDeclaredMadeForKids": False
        }
    }

    media_file = MediaFileUpload(video_path, resumable=True)
    request = service.videos().insert(
        part="snippet,status",
        body=request_body,
        media_body=media_file
    )
    return request.execute()['id']


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
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        "-y", output_path
    ]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()
    return output_path


async def main():
    try:
        if not await acquire_lock():
            logger.error("Bot already running! Exiting...")
            sys.exit(1)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(shutdown(sig, loop))
            )

        Path("temp").mkdir(exist_ok=True)
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Critical error: {str(e)}")
    finally:
        await release_lock()
        for f in Path("temp").glob("*"):
            f.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())