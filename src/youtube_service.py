# src/youtube_service.py
import os
import json
import logging
import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from aiogram.filters import Command

from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage

from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from moviepy.editor import ImageClip, AudioFileClip

from .utils import (
    get_user_data,
    update_user_data,
    fernet,
    storage,
    run_subprocess,
    decrypt_user_data
)

logger = logging.getLogger(__name__)


class YouTubeService:
    class YouTubeStates(StatesGroup):
        OAUTH_FLOW = State()
        CONTENT_TYPE = State()
        MEDIA_UPLOAD = State()
        PHOTO_UPLOAD = State()
        AUDIO_UPLOAD = State()
        VIDEO_GENERATION = State()
        CHANNEL_SELECT = State()
        METADATA_INPUT = State()
        VPN_CHOICE = State()
        VPN_CONFIG_UPLOAD = State()
        MULTI_CHANNEL = State()

    def __init__(self, bot: Bot, dp: Dispatcher):
        self.bot = bot
        self.dp = dp
        self.states = self.YouTubeStates()

    async def get_valid_credentials(self, user_id: int) -> Optional[Credentials]:
        try:
            encrypted = await get_user_data(user_id, "youtube_token")
            if not encrypted:
                return None

            token_data = json.loads(encrypted.decode())
            expiry = datetime.fromisoformat(token_data["expiry"]).astimezone(timezone.utc)
            now = datetime.now(timezone.utc)

            if now > expiry - timedelta(minutes=5):
                credentials = Credentials(**token_data)
                credentials.refresh(Request())
                token_data.update({
                    "token": credentials.token,
                    "expiry": credentials.expiry.astimezone(timezone.utc).isoformat()
                })
                encrypted = fernet.encrypt(json.dumps(token_data).encode())
                await update_user_data(user_id, {"youtube_token": encrypted.decode()})

            return Credentials(**token_data)
        except Exception as e:
            logger.error(f"Credentials error: {str(e)}")
            return None

    async def upload_video(self, user_id: int, video_path: str, metadata: dict) -> str:
        credentials = await self.get_valid_credentials(user_id)
        if not credentials:
            raise ValueError("❌ Authentication required")

        youtube = build("youtube", "v3", credentials=credentials)
        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": metadata['title'],
                    "description": metadata['description'],
                    "tags": metadata['tags'],
                    "categoryId": "10"
                },
                "status": {
                    "privacyStatus": "private",
                    "publishAt": metadata.get('publish_time'),
                    "selfDeclaredMadeForKids": False
                }
            },
            media_body=MediaFileUpload(video_path)
        )
        return request.execute()["id"]

    async def get_youtube_channels(self, user_id: int) -> List[Tuple[str, str]]:
        try:
            credentials = await self.get_valid_credentials(user_id)
            if not credentials:
                return []

            youtube = build("youtube", "v3", credentials=credentials)
            request = youtube.channels().list(
                part="snippet",
                mine=True,
                managedByMe=True
            )
            response = request.execute()
            return [
                (item["id"], item["snippet"]["title"])
                for item in response.get("items", [])
            ]
        except Exception as e:
            logger.error(f"Channel fetch error: {str(e)}")
            return []

    async def handle_auth_start(self, message: Message, state: FSMContext):
        await state.clear()
        await message.answer("📤 Отправьте файл client_secrets.json.")
        await state.set_state(self.states.OAUTH_FLOW)

    async def handle_oauth_file(self, message: Message, state: FSMContext):
        try:
            file = await self.bot.get_file(message.document.file_id)
            path = Path("temp") / f"{message.from_user.id}_client.json"
            await self.bot.download_file(file.file_path, path)

            with open(path, "r") as f:
                data = json.load(f)
                client_config = data["installed"]

            flow = InstalledAppFlow.from_client_config(
                {"installed": client_config},
                ["https://www.googleapis.com/auth/youtube"],
                redirect_uri="urn:ietf:wg:oauth:2.0:oob"
            )
            auth_url, _ = flow.authorization_url(prompt="consent")

            await state.update_data(client_config=client_config)
            await message.answer(f"🔑 Авторизуйтесь по ссылке: {auth_url}\nОтправьте код авторизации")
            path.unlink()

        except Exception as e:
            await message.answer(f"❌ Ошибка: {str(e)}")

    async def handle_oauth_code(self, message: Message, state: FSMContext):
        try:
            data = await state.get_data()
            flow = InstalledAppFlow.from_client_config(
                {"installed": data["client_config"]},
                ["https://www.googleapis.com/auth/youtube"],
                redirect_uri="urn:ietf:wg:oauth:2.0:oob"
            )

            flow.fetch_token(code=message.text.strip())
            credentials = flow.credentials

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
            await message.answer("✅ Авторизация успешна! Используйте /upload")
            await state.clear()

            channels = await self.get_youtube_channels(message.from_user.id)
            if channels:
                await self.show_channel_selection(message, channels, state)

        except Exception as e:
            await message.answer(f"❌ Ошибка авторизации: {str(e)}")

    async def show_channel_selection(self, message: Message, channels: List[Tuple[str, str]], state: FSMContext):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=id)]
            for id, name in channels
        ])
        await message.answer("📡 Выберите канал:", reply_markup=keyboard)
        await state.set_state(self.states.CHANNEL_SELECT)

    async def handle_channel_selection(self, callback: CallbackQuery, state: FSMContext):
        channel_id = callback.data
        channels = await self.get_youtube_channels(callback.from_user.id)
        channel_name = next((name for id, name in channels if id == channel_id), "Неизвестный канал")

        await state.update_data(selected_channel=channel_id)
        await callback.message.edit_text(f"✅ Выбран канал: {channel_name}")
        await self.show_content_type_menu(callback.message, state)

    async def show_content_type_menu(self, message: Message, state: FSMContext):  # <-- Добавить state
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Готовое видео", callback_data="ready_video"),
                InlineKeyboardButton(text="Фото + MP3", callback_data="photo_audio")
            ],
            [InlineKeyboardButton(text="Мультиканальная загрузка", callback_data="multi_channel")]
        ])
        await message.answer("📤 Выберите тип контента:", reply_markup=keyboard)
        await state.set_state(self.states.CONTENT_TYPE)

    async def handle_media_upload(self, message: Message, state: FSMContext):
        try:
            video = message.video
            file = await self.bot.get_file(video.file_id)
            path = Path("temp") / f"{message.from_user.id}_video.mp4"
            await self.bot.download_file(file.file_path, path)

            await message.answer("⏳ Видео загружается...")
            data = await state.get_data()
            metadata = data.get("video_metadata", {})

            video_id = await self.upload_video(
                user_id=message.from_user.id,
                video_path=str(path),
                metadata=metadata
            )

            await message.answer(f"✅ Видео загружено! ID: {video_id}")
            await state.clear()

        except Exception as e:
            await message.answer(f"❌ Ошибка загрузки: {str(e)}")
        finally:
            if path.exists():
                path.unlink()

    async def generate_video(self, user_id: int, state: FSMContext):
        data = await state.get_data()
        output_path = Path("temp") / f"{user_id}_video.mp4"

        try:
            audio = AudioFileClip(data["audio_path"])
            clip = ImageClip(data["photo_path"]).set_duration(audio.duration)
            clip = clip.set_audio(audio)
            clip.write_videofile(str(output_path), fps=24)

            await state.update_data(video_path=str(output_path))
            await self.bot.send_message(
                user_id,
                "✅ Видео готово!\nВведите метаданные в формате:\n"
                "Название\nОписание\nТеги (через запятую)\nДата публикации (YYYY-MM-DDTHH:MM:SSZ или 'сейчас')"
            )
            await state.set_state(self.states.METADATA_INPUT)

        except Exception as e:
            await self.bot.send_message(user_id, f"❌ Ошибка генерации: {str(e)}")

    async def handle_metadata_input(self, message: Message, state: FSMContext):
        try:
            parts = message.text.split('\n')
            if len(parts) != 4:
                raise ValueError("Неверный формат")

            title = parts[0].strip()
            description = parts[1].strip()
            tags = [tag.strip() for tag in parts[2].split(',')]
            publish_time = parts[3].strip().lower()

            if publish_time != 'сейчас':
                publish_time = datetime.fromisoformat(publish_time).isoformat()
            else:
                publish_time = datetime.now(timezone.utc).isoformat()

            await state.update_data(video_metadata={
                'title': title,
                'description': description,
                'tags': tags,
                'publish_time': publish_time,
                'is_scheduled': publish_time != 'сейчас'
            })

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Да", callback_data="use_vpn"),
                 InlineKeyboardButton(text="Нет", callback_data="no_vpn")]
            ])
            await message.answer("🔐 Использовать VPN для загрузки?", reply_markup=keyboard)
            await state.set_state(self.states.VPN_CHOICE)

        except Exception as e:
            await message.answer(f"❌ Ошибка формата: {str(e)}")

    def setup_routes(self):
        self.dp.message.register(self.handle_auth_start, Command("auth"))
        self.dp.message.register(self.handle_oauth_file, self.states.OAUTH_FLOW, F.document)
        self.dp.message.register(self.handle_oauth_code, self.states.OAUTH_FLOW)
        self.dp.message.register(self.handle_media_upload, self.states.MEDIA_UPLOAD, F.video)

        self.dp.callback_query.register(
            self.handle_channel_selection,
            self.states.CHANNEL_SELECT
        )

        self.dp.message.register(
            self.handle_metadata_input,
            self.states.METADATA_INPUT
        )

        self.dp.callback_query.register(
            lambda c: self.handle_vpn_choice(c, self.states.VPN_CHOICE),
            F.data.in_(["use_vpn", "no_vpn"])
        )

    async def handle_vpn_choice(self, callback: CallbackQuery, state: FSMContext):
        if callback.data == "use_vpn":
            await callback.message.answer("📤 Отправьте VPN-конфиг (.ovpn) с названием в подписи")
            await state.set_state(self.states.VPN_CONFIG_UPLOAD)
        else:
            await state.update_data(vpn_config=None)
            await self.handle_channel_select(callback.message, state)
        await callback.answer()

    async def handle_vpn_config_upload(self, message: Message, state: FSMContext):
        try:
            config_name = message.caption.strip().split('\n')[0].strip()
            file = await self.bot.get_file(message.document.file_id)
            path = Path("temp") / f"{message.from_user.id}_vpn.ovpn"
            await self.bot.download_file(file.file_path, path)

            with open(path, 'r') as f:
                config_data = f.read()

            if "client" not in config_data:
                raise ValueError("Invalid OVPN config")

            await state.update_data(vpn_config={'name': config_name, 'data': config_data})
            path.unlink()

            await message.answer(f"✅ Конфиг '{config_name}' сохранен!")
            await self.handle_channel_select(message, state)

        except Exception as e:
            await message.answer(f"❌ Ошибка: {str(e)}")

    async def handle_channel_select(self, message: Message, state: FSMContext):
        channels = await self.get_youtube_channels(message.from_user.id)
        if not channels:
            await message.answer("❌ Нет доступных каналов!")
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=id)]
            for id, name in channels
        ])
        await message.answer("📡 Выберите канал:", reply_markup=keyboard)
        await state.set_state(self.states.CHANNEL_SELECT)