# src/instagram_service.py
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from pathlib import Path

from aiogram.filters import Command
import json

from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    TwoFactorRequired,
    ChallengeRequired,
    ClientError
)

from aiogram import Bot, Dispatcher, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from .utils import (
    get_user_data,
    update_user_data,
    fernet,
    storage,
    decrypt_user_data
)

logger = logging.getLogger(__name__)


class InstagramService:
    class InstagramStates(StatesGroup):
        AUTH_START = State()
        CREDENTIALS_INPUT = State()
        TWO_FACTOR_INPUT = State()
        TIME_RANGE_INPUT = State()
        PROCESSING = State()

    def __init__(self, bot: Bot, dp: Dispatcher):
        self.bot = bot
        self.dp = dp
        self.states = self.InstagramStates()
        self.setup_handlers()

    def setup_handlers(self):
        self.dp.message.register(
            self.handle_instagram_start,
            Command("instagram")
        )
        self.dp.message.register(
            self.handle_credentials_input,
            self.states.CREDENTIALS_INPUT
        )
        self.dp.message.register(
            self.handle_two_factor_input,
            self.states.TWO_FACTOR_INPUT
        )
        self.dp.message.register(
            self.handle_time_range_input,
            self.states.TIME_RANGE_INPUT
        )

    async def handle_instagram_start(self, message: Message, state: FSMContext):
        await message.answer(
            "📩 Введите ваш Instagram логин и пароль в формате:\n"
            "login:ваш_логин\npassword:ваш_пароль"
        )
        await state.set_state(self.states.CREDENTIALS_INPUT)

    async def handle_credentials_input(self, message: Message, state: FSMContext):
        try:
            credentials = {}
            for line in message.text.split('\n'):
                key, value = line.split(':', 1)
                credentials[key.strip().lower()] = value.strip()

            await state.update_data(credentials=credentials)
            await self.instagram_auth(message.from_user.id, state)

        except Exception as e:
            await message.answer(f"❌ Ошибка формата: {str(e)}")

    async def instagram_auth(self, user_id: int, state: FSMContext):
        data = await state.get_data()
        credentials = data['credentials']
        cl = Client()

        try:
            await self.bot.send_message(user_id, "🔐 Пытаюсь войти в аккаунт...")
            cl.login(credentials['login'], credentials['password'])
            await self.save_session(user_id, cl)
            await self.bot.send_message(user_id, "✅ Успешная авторизация!")
            await self.request_time_range(user_id, state)

        except TwoFactorRequired as e:
            await state.set_state(self.states.TWO_FACTOR_INPUT)
            await self.bot.send_message(
                user_id,
                "🔑 Введите код двухфакторной аутентификации:"
            )

        except (LoginRequired, ChallengeRequired, ClientError) as e:
            await self.handle_auth_error(user_id, e)
            await state.clear()

    async def handle_two_factor_input(self, message: Message, state: FSMContext):
        user_id = message.from_user.id  # Добавляем получение user_id
        data = await state.get_data()
        credentials = data['credentials']
        cl = Client()

        try:
            cl.login(
                credentials['login'],
                credentials['password'],
                verification_code=message.text.strip()
            )
            await self.save_session(user_id, cl)  # Теперь user_id определен
            await self.bot.send_message(user_id, "✅ Успешная авторизация!")
            await self.request_time_range(user_id, state)

        except Exception as e:
            await self.handle_auth_error(user_id, e)  # Используем полученный user_id
            await state.clear()

    async def save_session(self, user_id: int, client: Client):
        session_data = client.get_settings()
        encrypted = fernet.encrypt(json.dumps(session_data).encode())
        await update_user_data(user_id, {"instagram_session": encrypted.decode()})

    async def request_time_range(self, user_id: int, state: FSMContext):
        await self.bot.send_message(
            user_id,
            "⏳ Введите временной диапазон в часах (макс. 168):"
        )
        await state.set_state(self.states.TIME_RANGE_INPUT)

    async def handle_time_range_input(self, message: Message, state: FSMContext):
        try:
            hours = int(message.text)
            if not 1 <= hours <= 168:
                raise ValueError("Диапазон должен быть от 1 до 168 часов")

            await state.update_data(hours=hours)
            await self.process_instagram_data(message.from_user.id, state)

        except Exception as e:
            await message.answer(f"❌ Некорректное значение: {str(e)}")

    async def process_instagram_data(self, user_id: int, state: FSMContext):
        try:
            data = await state.get_data()
            hours = data['hours']
            cl = await self.load_session(user_id)

            await self.bot.send_message(user_id, "⏳ Собираю сообщения...")
            messages = await self.get_recent_messages(cl, hours)

            report = self.generate_report(messages)
            await self.send_report(user_id, report)

        except Exception as e:
            await self.handle_processing_error(user_id, e)
        finally:
            await state.clear()

    async def load_session(self, user_id: int) -> Client:
        encrypted = await get_user_data(user_id, "instagram_session")
        if not encrypted:
            raise ValueError("Сессия не найдена")

        session_data = json.loads(fernet.decrypt(encrypted.encode()).decode())
        cl = Client()
        cl.set_settings(session_data)
        return cl

    async def get_recent_messages(self, client: Client, hours: int) -> List[Dict]:
        threads = client.direct_threads()
        cutoff = datetime.now() - timedelta(hours=hours)

        messages = []
        for thread in threads:
            for msg in client.direct_messages(thread.id):
                if msg.timestamp >= cutoff.timestamp():
                    messages.append({
                        'user': thread.users[0].username,
                        'text': msg.text,
                        'timestamp': msg.timestamp
                    })
        return messages

    def generate_report(self, messages: List[Dict]) -> str:
        if not messages:
            return "📭 Нет сообщений за выбранный период"

        report = ["📨 Последние сообщения:"]
        for msg in sorted(messages, key=lambda x: x['timestamp'], reverse=True)[:50]:
            dt = datetime.fromtimestamp(msg['timestamp'])
            report.append(
                f"{dt.strftime('%d.%m.%Y %H:%M')} "
                f"@{msg['user']}: {msg['text'][:100]}"
            )
        return "\n".join(report)

    async def send_report(self, user_id: int, report: str):
        chunks = [report[i:i + 4000] for i in range(0, len(report), 4000)]
        for chunk in chunks:
            await self.bot.send_message(user_id, chunk)

    async def handle_auth_error(self, user_id: int, error: Exception):
        error_msg = {
            LoginRequired: "❌ Ошибка авторизации: Неверные учетные данные",
            ChallengeRequired: "🔒 Требуется проверка в приложении Instagram",
            ClientError: f"🚫 Ошибка клиента: {str(error)}"
        }.get(type(error), f"⚠️ Неизвестная ошибка: {str(error)}")

        await self.bot.send_message(user_id, error_msg)

    async def handle_processing_error(self, user_id: int, error: Exception):
        await self.bot.send_message(
            user_id,
            f"⚠️ Ошибка обработки: {str(error)}\nПопробуйте снова /instagram"
        )