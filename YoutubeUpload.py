import os
import logging
import tempfile
import subprocess
import asyncio
from pathlib import Path
from typing import Dict

from cryptography.fernet import Fernet
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow

# Инициализация бота
bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
storage = RedisStorage.from_url(os.getenv("REDIS_URL"))
dp = Dispatcher(storage=storage)
fernet = Fernet(os.getenv("ENCRYPTION_KEY"))


# Класс состояний
class UploadStates(StatesGroup):
    CONTENT_TYPE = State()
    MEDIA_UPLOAD = State()
    METADATA = State()
    VPN_CONFIG = State()
    PROXY = State()
    YOUTUBE_TOKEN = State()


# Хранилище данных пользователя
async def get_user_data(user_id: int) -> Dict:
    return await storage.redis.hgetall(f"user:{user_id}")


async def update_user_data(user_id: int, data: Dict) -> None:
    await storage.redis.hmset(f"user:{user_id}", data)


# Обработчики
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🎥 YouTube Upload Bot\n\n"
        "Отправьте /upload чтобы начать загрузку",
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
    await state.update_data(content_type=callback.data)

    if callback.data == "video":
        await callback.message.answer("📤 Отправьте видео файл (MP4)")
    else:
        await callback.message.answer("🎵 Отправьте аудио файл (MP3)")

    await state.set_state(UploadStates.MEDIA_UPLOAD)
    await callback.answer()


@dp.message(UploadStates.MEDIA_UPLOAD, F.video | F.audio | F.photo)
async def media_handler(message: types.Message, state: FSMContext, bot: Bot):
    user_data = await state.get_data()

    try:
        # Обработка видео
        if message.video:
            file = await bot.get_file(message.video.file_id)
            ext = Path(file.file_path).suffix
            path = f"temp/{message.from_user.id}_video{ext}"
            await bot.download_file(file.file_path, path)
            await state.update_data(video_path=path)

        # Обработка аудио
        elif message.audio:
            file = await bot.get_file(message.audio.file_id)
            path = f"temp/{message.from_user.id}_audio.mp3"
            await bot.download_file(file.file_path, path)
            await state.update_data(audio_path=path)
            await message.answer("🖼 Теперь отправьте изображение")

        # Обработка изображения
        elif message.photo:
            file = await bot.get_file(message.photo[-1].file_id)
            path = f"temp/{message.from_user.id}_image.jpg"
            await bot.download_file(file.file_path, path)
            await state.update_data(image_path=path)

        # Проверка завершенности
        data = await state.get_data()
        if (user_data.get('content_type') == 'video' and 'video_path' in data) or \
                (user_data.get('content_type') == 'audio_image' and 'audio_path' in data and 'image_path' in data):
            await state.set_state(UploadStates.METADATA)
            await message.answer(
                "📝 Введите метаданные в формате:\n"
                "<b>Название</b>\n"
                "<b>Описание</b>\n"
                "<b>Теги</b> (через запятую)\n"
                "<b>Дата публикации</b> (YYYY-MM-DDTHH:MM:SSZ или 'сейчас')",
                parse_mode="HTML"
            )

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


@dp.message(UploadStates.METADATA)
async def metadata_handler(message: types.Message, state: FSMContext):
    try:
        parts = message.text.split('\n')
        metadata = {
            'title': parts[0],
            'description': parts[1],
            'tags': parts[2].split(','),
            'publish_at': parts[3] if len(parts) > 3 else 'сейчас'
        }
        await state.update_data(metadata=metadata)

        await message.answer(
            "⚙️ Хотите настроить VPN/Прокси?",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🛡️ Да", callback_data="setup_network")],
                [types.InlineKeyboardButton(text="🚀 Пропустить", callback_data="skip_network")]
            ])
        )

    except Exception as e:
        await message.answer(f"❌ Ошибка формата: {str(e)}\nПопробуйте еще раз")


@dp.callback_query(F.data == "setup_network")
async def setup_network(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🌐 Выберите тип подключения:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔐 VPN", callback_data="setup_vpn")],
            [types.InlineKeyboardButton(text="🔗 Прокси", callback_data="setup_proxy")]
        ])
    )
    await callback.answer()


@dp.callback_query(F.data == "setup_vpn")
async def setup_vpn(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📎 Отправьте файл конфига VPN и укажите тип в формате:\n"
        "<code>тип;название</code>\n\n"
        "Пример:\n"
        "<code>openvpn;Мой VPN</code>",
        parse_mode="HTML"
    )
    await state.set_state(UploadStates.VPN_CONFIG)
    await callback.answer()


@dp.message(UploadStates.VPN_CONFIG, F.document)
async def vpn_config_handler(message: types.Message, state: FSMContext, bot: Bot):
    try:
        vpn_type, name = message.caption.split(';')
        file = await bot.get_file(message.document.file_id)
        path = f"temp/{message.from_user.id}_vpn.conf"

        await bot.download_file(file.file_path, path)
        with open(path, 'rb') as f:
            encrypted = fernet.encrypt(f.read())

        await update_user_data(
            message.from_user.id,
            {f"vpn:{name}": encrypted.decode()}
        )
        await message.answer(f"✅ VPN '{name}' сохранен!")
        os.unlink(path)

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


@dp.callback_query(F.data == "setup_proxy")
async def setup_proxy(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🔗 Введите прокси в формате:\n"
        "<code>тип://логин:пароль@хост:порт</code>\n\n"
        "Пример:\n"
        "<code>socks5://user123:pass456@1.2.3.4:1080</code>",
        parse_mode="HTML"
    )
    await state.set_state(UploadStates.PROXY)
    await callback.answer()


@dp.message(UploadStates.PROXY)
async def proxy_handler(message: types.Message, state: FSMContext):
    try:
        if not message.text.startswith(('http://', 'https://', 'socks5://')):
            raise ValueError("Неверный формат прокси")

        encrypted = fernet.encrypt(message.text.encode()).decode()
        await update_user_data(message.from_user.id, {"proxy": encrypted})
        await message.answer("✅ Прокси сохранен!")

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


@dp.callback_query(F.data == "skip_network")
async def skip_network(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("📎 Отправьте файл YouTube токена (JSON)")
    await state.set_state(UploadStates.YOUTUBE_TOKEN)
    await callback.answer()


@dp.message(UploadStates.YOUTUBE_TOKEN, F.document)
async def token_handler(message: types.Message, state: FSMContext, bot: Bot):
    try:
        file = await bot.get_file(message.document.file_id)
        path = f"temp/{message.from_user.id}_token.json"

        await bot.download_file(file.file_path, path)
        with open(path, 'rb') as f:
            encrypted = fernet.encrypt(f.read())

        await update_user_data(message.from_user.id, {"youtube_token": encrypted.decode()})
        await message.answer("✅ Токен сохранен!")
        os.unlink(path)

        await start_upload_process(message, state)

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")


async def start_upload_process(message: types.Message, state: FSMContext):
    try:
        user_data = await get_user_data(message.from_user.id)
        state_data = await state.get_data()

        # Расшифровка данных
        proxy = (
            fernet.decrypt(user_data[b'proxy']).decode()
            if b'proxy' in user_data
            else None
        )
        token = fernet.decrypt(user_data[b'youtube_token']).decode()

        # Применение прокси
        if proxy:
            os.environ['HTTP_PROXY'] = proxy
            os.environ['HTTPS_PROXY'] = proxy

        # Подключение VPN
        if any(key.startswith(b'vpn:') for key in user_data.keys()):
            for key, value in user_data.items():
                if key.startswith(b'vpn:'):
                    decrypted = fernet.decrypt(value).decode()
                    with tempfile.NamedTemporaryFile(delete=False) as f:
                        f.write(decrypted.encode())
                        vpn_path = f.name

                    vpn_type = key.decode().split(':')[1]
                    subprocess.run(
                        [
                            "openvpn" if vpn_type == "openvpn" else "wg-quick",
                            "up" if vpn_type == "wireguard" else "--config",
                            vpn_path
                        ],
                        check=True
                    )
                    os.unlink(vpn_path)

        # Создание видео
        if state_data['content_type'] == 'audio_image':
            video_path = await create_video_from_media(
                state_data['image_path'],
                state_data['audio_path']
            )
        else:
            video_path = state_data['video_path']

        # Загрузка на YouTube
        service = build_youtube_service(token)
        video_id = upload_video(
            service,
            video_path,
            state_data['metadata']
        )

        await message.answer(f"✅ Видео успешно загружено! ID: {video_id}")

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

    finally:
        # Очистка временных файлов
        for file in ['video_path', 'audio_path', 'image_path']:
            if file in state_data:
                os.unlink(state_data[file])
        await state.clear()


def build_youtube_service(token_path: str):
    flow = InstalledAppFlow.from_client_secrets_file(
        token_path,
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    credentials = flow.run_local_server(port=8080)
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
    response = request.execute()
    return response['id']


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
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()
    return output_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    Path("temp").mkdir(exist_ok=True)
    asyncio.run(dp.start_polling(bot))