import os
import threading
import ffmpeg
import logging
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from datetime import datetime
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from telegram import Bot
import time  # Добавлен импорт для паузы между загрузками
import pytz
from datetime import datetime

def convert_to_iso8601(user_input, timezone_str="Europe/Moscow"):
    """
    Преобразует введенную дату в формат ISO 8601 (UTC) для YouTube API.
    user_input - строка в формате "ДД.ММ.ГГГГ ЧЧ:ММ"
    """
    try:
        local_tz = pytz.timezone(timezone_str)  # Часовой пояс пользователя
        dt_local = datetime.strptime(user_input, "%d.%m.%Y %H:%M")  # Преобразуем строку в datetime
        dt_local = local_tz.localize(dt_local)  # Добавляем информацию о часовом поясе
        dt_utc = dt_local.astimezone(pytz.utc)  # Конвертируем в UTC

        return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")  # Возвращаем ISO 8601

    except ValueError:
        return None  # Ошибка в формате даты

# Логирование
logging.basicConfig(filename='youtube_bot.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')


def log_info(message):
    print(f"INFO: {message}")
    logging.info(message)


def log_error(message):
    print(f"ERROR: {message}")
    logging.error(message)


# YouTube API
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def authenticate_youtube_account(token_file):
    flow = InstalledAppFlow.from_client_secrets_file(token_file, SCOPES)
    credentials = flow.run_local_server(port=8080)
    youtube = build(API_SERVICE_NAME, API_VERSION, credentials=credentials)
    log_info(f"Аутентификация для {token_file} прошла успешно.")
    return youtube
import ffmpeg
import mutagen

def get_audio_duration(mp3_path):
    """Определяет длину MP3-файла в секундах"""
    audio = mutagen.File(mp3_path)
    return int(audio.info.length) if audio and audio.info else 60  # Если ошибка, длительность по умолчанию = 60 сек.

def create_video(mp3_path, image_path, output_path):
    """Создает видео из MP3 и изображения с длиной, соответствующей аудио"""
    duration = get_audio_duration(mp3_path)  # Определяем длину аудиофайла

    video_input = ffmpeg.input(image_path, loop=1, framerate=1, t=duration)  # Устанавливаем длину видео = длине аудио
    audio_input = ffmpeg.input(mp3_path)

    ffmpeg.output(video_input, audio_input, output_path, vcodec='libx264', acodec='aac', strict='experimental') \
        .overwrite_output().run()
    log_info(f"Видео создано ({duration} сек): {output_path}")


def upload_video(youtube, video_path, title, description, tags, publish_at=None, privacy_status="private"):
    try:
        request_body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": "10"
            },
            "status": {
                "privacyStatus": privacy_status,
                "madeForKids": False,
                "selfDeclaredMadeForKids": False
            }
        }

        if publish_at:
            request_body["status"]["publishAt"] = publish_at  # Убираем ":00Z", т.к. оно уже в правильном формате

        media_file = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=request_body, media_body=media_file)
        response = request.execute()

        log_info(f"✅ Видео {response['id']} успешно загружено!")
        send_telegram_notification(f"✅ Видео {response['id']} загружено на YouTube.")

    except HttpError as e:
        log_error(f"❌ Ошибка при загрузке видео: {e}")
        send_telegram_notification(f"❌ Ошибка загрузки видео: {e}")


def send_telegram_notification(message):
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "your_default_token")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "your_default_chat_id")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
    log_info("Telegram уведомление отправлено.")


def gui_interface():
    root = tk.Tk()
    root.title("YouTube Video Upload Bot")

    canvas = tk.Canvas(root)
    scrollbar = tk.Scrollbar(root, orient="vertical", command=canvas.yview)
    scrollable_frame = tk.Frame(canvas)

    scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    channels = []

    for i in range(5):
        frame = tk.LabelFrame(scrollable_frame, text=f"Канал {i + 1}", padx=10, pady=10)
        frame.pack(padx=10, pady=5, fill="x")

        mp3_var = tk.StringVar()
        images_var = tk.StringVar()
        token_var = tk.StringVar()
        title_var = tk.StringVar()
        description_var = tk.StringVar()
        tags_var = tk.StringVar()
        publish_var = tk.StringVar()

        def browse_mp3_file(entry):
            filename = filedialog.askopenfilename(filetypes=[("MP3 Files", "*.mp3")])
            entry.set(filename)

        def browse_images_folder(entry):
            foldername = filedialog.askdirectory()
            entry.set(foldername)

        def browse_token_file(entry):
            filename = filedialog.askopenfilename(filetypes=[("JSON Files", "*.json")])
            entry.set(filename)

        def get_publish_time(entry):
            publish_choice = simpledialog.askstring("Отложенная публикация", "Опубликовать сейчас? (да/нет)")

            if not publish_choice:
                return

            if publish_choice.lower() == 'да':
                entry.set("")  # Публикуем сразу
                return

            publish_date = simpledialog.askstring("Введите дату", "Формат: ДД.ММ.ГГГГ ЧЧ:ММ")

            if not publish_date:
                return

            iso_date = convert_to_iso8601(publish_date)  # Конвертация в ISO 8601
            if iso_date:
                entry.set(iso_date)  # Устанавливаем корректное время публикации
            else:
                messagebox.showerror("Ошибка", "Неверный формат даты/времени. Попробуйте снова.")
                get_publish_time(entry)  # Повторный ввод, если ошибка

        tk.Label(frame, text="MP3 Файл:").pack(anchor="w")
        tk.Entry(frame, textvariable=mp3_var, width=50).pack()
        tk.Button(frame, text="Выбрать MP3", command=lambda e=mp3_var: browse_mp3_file(e)).pack()

        tk.Label(frame, text="Папка с изображениями:").pack(anchor="w")
        tk.Entry(frame, textvariable=images_var, width=50).pack()
        tk.Button(frame, text="Выбрать папку", command=lambda e=images_var: browse_images_folder(e)).pack()

        tk.Label(frame, text="Файл токена:").pack(anchor="w")
        tk.Entry(frame, textvariable=token_var, width=50).pack()
        tk.Button(frame, text="Выбрать токен", command=lambda e=token_var: browse_token_file(e)).pack()

        tk.Label(frame, text="Название видео:").pack(anchor="w")
        tk.Entry(frame, textvariable=title_var, width=50).pack()

        tk.Label(frame, text="Описание:").pack(anchor="w")
        tk.Entry(frame, textvariable=description_var, width=50).pack()

        tk.Label(frame, text="Теги (через запятую):").pack(anchor="w")
        tk.Entry(frame, textvariable=tags_var, width=50).pack()

        tk.Button(frame, text="Выбрать время публикации", command=lambda e=publish_var: get_publish_time(e)).pack()

        channels.append({
            "mp3": mp3_var,
            "images": images_var,
            "token": token_var,
            "title": title_var,
            "description": description_var,
            "tags": tags_var,
            "publish": publish_var
        })

    def start_bot():
        uploaded_any = False

        for i, channel in enumerate(channels):
            mp3, images, token, title, description, tags, publish = [channel[key].get().strip() for key in channel]

            if not mp3 or not images or not token or not title or not description:
                continue

            uploaded_any = True
            video_output = f"output_video_{i + 1}.mp4"
            image_path = next((os.path.join(images, f"{i + 1}{ext}") for ext in [".jpg", ".png"] if os.path.exists(os.path.join(images, f"{i + 1}{ext}"))), None)

            if not image_path:
                continue

            create_video(mp3, image_path, video_output)
            youtube = authenticate_youtube_account(token)
            threading.Thread(target=upload_video, args=(youtube, video_output, title, description, tags.split(","), publish)).start()
            time.sleep(1)  # Пауза между загрузками

        if not uploaded_any:
            messagebox.showwarning("Предупреждение", "Ни одно видео не загружено!")

    tk.Button(scrollable_frame, text="Начать загрузку", command=start_bot).pack(pady=10)
    root.mainloop()


if __name__ == "__main__":
    gui_interface()