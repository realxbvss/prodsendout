import os
import platform
import threading
import logging
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
import time
import subprocess
import shutil
import tempfile
from pathlib import Path

# Автоматическое определение пути к FFmpeg
FFMPEG_PATH = r'C:\ffmpeg\bin\ffmpeg.exe' if platform.system() == 'Windows' else '/opt/homebrew/bin/ffmpeg'

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Глобальные переменные
vpn_configs = []
proxy_addresses = []


def check_ffmpeg():
    """Проверяет доступность FFmpeg с учетом платформы"""
    try:
        if platform.system() == 'Darwin':
            # Для macOS проверяем через which
            result = subprocess.run(['which', 'ffmpeg'],
                                    capture_output=True,
                                    text=True)
            if result.returncode != 0:
                raise FileNotFoundError
        else:
            subprocess.run([FFMPEG_PATH, '-version'],
                           check=True,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        return True
    except Exception as e:
        logging.error(f"FFmpeg error: {str(e)}")
        msg = ("FFmpeg не найден! Для macOS выполните:\n"
               "1. Установите Homebrew:\n"
               "/bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"\n"
               "2. Установите FFmpeg: brew install ffmpeg")
        messagebox.showerror("Ошибка FFmpeg", msg)
        return False


def create_video_from_media(image_path, audio_path, output_path):
    """Создает видео из изображения и аудио"""
    try:
        cmd = [
            FFMPEG_PATH,
            '-loop', '1',
            '-i', image_path,
            '-i', audio_path,
            '-c:v', 'libx264',
            '-tune', 'stillimage',
            '-c:a', 'aac',
            '-b:a', '192k',
            '-pix_fmt', 'yuv420p',
            '-shortest',
            '-y',
            output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        error_msg = f"FFmpeg error: {e.stderr.decode()}"
        logging.error(error_msg)
        messagebox.showerror("Ошибка FFmpeg", error_msg)
        return False


def connect_to_vpn(vpn_type, config_file):
    """Подключение VPN"""
    try:
        if vpn_type == "OpenVPN":
            if not shutil.which("openvpn"):
                logging.warning("OpenVPN не установлен. Пропуск подключения.")
                return
            subprocess.run(["openvpn", "--config", config_file], check=True)
        elif vpn_type == "WireGuard":
            if not shutil.which("wg-quick"):
                logging.warning("WireGuard не установлен. Пропуск подключения.")
                return
            subprocess.run(["wg-quick", "up", config_file], check=True)
        logging.info(f"VPN ({vpn_type}) подключен успешно!")
    except subprocess.CalledProcessError as e:
        logging.error(f"Ошибка при подключении VPN: {e}")


def disconnect_vpn(vpn_type, config_file):
    """Отключение VPN"""
    try:
        if vpn_type == "OpenVPN":
            subprocess.run(["pkill", "openvpn"], check=True)
        elif vpn_type == "WireGuard":
            subprocess.run(["wg-quick", "down", config_file], check=True)
        logging.info(f"VPN ({vpn_type}) отключен успешно!")
    except subprocess.CalledProcessError as e:
        logging.error(f"Ошибка отключения VPN: {e}")


def set_proxy(proxy_address):
    """Установка прокси"""
    os.environ["http_proxy"] = proxy_address
    os.environ["https_proxy"] = proxy_address
    logging.info(f"Прокси установлен: {proxy_address}")


def authenticate_youtube_account(token_file):
    """Аутентификация YouTube"""
    flow = InstalledAppFlow.from_client_secrets_file(
        token_file,
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    credentials = flow.run_local_server(port=8080)
    return build("youtube", "v3", credentials=credentials)


def upload_video(youtube, video_path, title, description, tags, publish_at=None):
    """Загрузка видео на YouTube"""
    request_body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "10"
        },
        "status": {
            "privacyStatus": "public",
            "madeForKids": False,
            "selfDeclaredMadeForKids": False
        }
    }
    if publish_at:
        request_body["status"]["publishAt"] = publish_at

    media_file = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=request_body,
        media_body=media_file
    )
    response = request.execute()
    logging.info(f"Видео '{title}' загружено! ID: {response['id']}")


def cleanup():
    """Очистка ресурсов"""
    for vpn_type, config in vpn_configs:
        disconnect_vpn(vpn_type, config)
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    logging.info("Ресурсы очищены")


def gui_interface():
    """Графический интерфейс с прокруткой"""
    root = tk.Tk()
    root.title("YouTube Video Upload Bot")
    root.withdraw()

    try:
        if not check_ffmpeg():
            root.destroy()
            return

        root.deiconify()

        # Основной контейнер с прокруткой
        main_frame = tk.Frame(root)
        main_frame.pack(fill=tk.BOTH, expand=1)

        # Создаем Canvas и Scrollbar
        canvas = tk.Canvas(main_frame)
        scrollbar = tk.Scrollbar(main_frame, orient=tk.VERTICAL, command=canvas.yview)
        scrollable_frame = tk.Frame(canvas)

        # Настройка прокрутки
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(
                scrollregion=canvas.bbox("all")
            )
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Упаковка элементов прокрутки
        main_frame.pack(fill=tk.BOTH, expand=1)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=1)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        channels = []

        # Настройка VPN
        if messagebox.askyesno("VPN", "Использовать VPN?"):
            num_vpns = simpledialog.askinteger("VPN", "Количество VPN (1-10):", minvalue=1, maxvalue=10)
            for i in range(num_vpns):
                vpn_type = simpledialog.askstring("VPN", "Тип (OpenVPN/WireGuard):")
                config_file = filedialog.askopenfilename(title=f"Конфиг {vpn_type} {i + 1}")
                if config_file:
                    vpn_configs.append((vpn_type, config_file))

        # Настройка прокси
        if messagebox.askyesno("Прокси", "Использовать прокси?"):
            num_proxies = simpledialog.askinteger("Прокси", "Количество прокси (1-10):", minvalue=1, maxvalue=10)
            for i in range(num_proxies):
                proxy = simpledialog.askstring("Прокси", "Формат: http://user:pass@host:port")
                if proxy:
                    proxy_addresses.append(proxy)

        # Настройка каналов
        num_channels = simpledialog.askinteger("Каналы", "Количество каналов (1-10):", minvalue=1, maxvalue=10)
        if not num_channels:
            root.destroy()
            return

        for _ in range(num_channels):
            channels.append({
                'content_type': tk.StringVar(value='video'),
                'video_path': '',
                'audio_path': '',
                'image_path': '',
                'title_entry': None,
                'description_entry': None,
                'tags_entry': None,
                'publish_at_entry': None,
                'token_file': '',
                'video_label': None,
                'audio_label': None,
                'image_label': None,
                'token_label': None
            })

        # Создание элементов интерфейса внутри прокручиваемой области
        for channel_index in range(num_channels):
            frame = tk.Frame(scrollable_frame, bd=2, relief=tk.GROOVE)
            frame.pack(pady=10, padx=10, fill=tk.X)

            # Заголовок канала
            tk.Label(frame, text=f"Канал {channel_index + 1}", font=('Arial', 10, 'bold')).grid(row=0, column=0,
                                                                                                columnspan=3)

            # Выбор типа контента
            content_frame = tk.Frame(frame)
            content_frame.grid(row=1, column=0, columnspan=3, pady=5)
            tk.Radiobutton(content_frame, text="Готовое видео",
                           variable=channels[channel_index]['content_type'],
                           value='video',
                           command=lambda ci=channel_index: update_labels(ci)).pack(side=tk.LEFT)
            tk.Radiobutton(content_frame, text="Аудио + Изображение",
                           variable=channels[channel_index]['content_type'],
                           value='audio_image',
                           command=lambda ci=channel_index: update_labels(ci)).pack(side=tk.LEFT)

            # Элементы управления
            video_frame = tk.Frame(frame)
            video_frame.grid(row=2, column=0, columnspan=3, pady=5)
            tk.Button(video_frame, text="Выбрать видео",
                      command=lambda ci=channel_index: select_file(ci, 'video')).pack(side=tk.LEFT)
            channels[channel_index]['video_label'] = tk.Label(video_frame, text="Видео не выбрано", fg='red')
            channels[channel_index]['video_label'].pack(side=tk.LEFT, padx=10)

            media_frame = tk.Frame(frame)
            media_frame.grid(row=3, column=0, columnspan=3, pady=5)
            tk.Button(media_frame, text="Выбрать аудио",
                      command=lambda ci=channel_index: select_file(ci, 'audio')).pack(side=tk.LEFT)
            channels[channel_index]['audio_label'] = tk.Label(media_frame, text="Аудио не выбрано", fg='red')
            channels[channel_index]['audio_label'].pack(side=tk.LEFT, padx=10)

            tk.Button(media_frame, text="Выбрать изображение",
                      command=lambda ci=channel_index: select_file(ci, 'image')).pack(side=tk.LEFT)
            channels[channel_index]['image_label'] = tk.Label(media_frame, text="Изображение не выбрано", fg='red')
            channels[channel_index]['image_label'].pack(side=tk.LEFT, padx=10)

            # Метаданные
            meta_frame = tk.Frame(frame)
            meta_frame.grid(row=4, column=0, columnspan=3, pady=5)
            tk.Label(meta_frame, text="Название:").pack(side=tk.LEFT)
            title_entry = tk.Entry(meta_frame, width=40)
            title_entry.pack(side=tk.LEFT, padx=5)
            channels[channel_index]['title_entry'] = title_entry

            tk.Label(meta_frame, text="Описание:").pack(side=tk.LEFT)
            description_entry = tk.Entry(meta_frame, width=40)
            description_entry.pack(side=tk.LEFT, padx=5)
            channels[channel_index]['description_entry'] = description_entry

            # Теги и дата
            tags_frame = tk.Frame(frame)
            tags_frame.grid(row=5, column=0, columnspan=3, pady=5)
            tk.Label(tags_frame, text="Теги (через запятую):").pack(side=tk.LEFT)
            tags_entry = tk.Entry(tags_frame, width=40)
            tags_entry.pack(side=tk.LEFT, padx=5)
            channels[channel_index]['tags_entry'] = tags_entry

            tk.Label(tags_frame, text="Дата публикации:").pack(side=tk.LEFT)
            publish_entry = tk.Entry(tags_frame, width=20)
            publish_entry.pack(side=tk.LEFT, padx=5)
            channels[channel_index]['publish_at_entry'] = publish_entry

            # Токен
            token_frame = tk.Frame(frame)
            token_frame.grid(row=6, column=0, columnspan=3, pady=5)
            tk.Button(token_frame, text="Выбрать токен",
                      command=lambda ci=channel_index: select_file(ci, 'token')).pack(side=tk.LEFT)
            channels[channel_index]['token_label'] = tk.Label(token_frame, text="Токен не выбран", fg='red')
            channels[channel_index]['token_label'].pack(side=tk.LEFT, padx=10)

        def select_file(channel_index, file_type):
            """Обработка выбора файлов"""
            filetypes = {
                'video': [("MP4", "*.mp4")],
                'image': [("Изображения", "*.jpg *.jpeg *.png")],
                'audio': [("Аудио", "*.mp3")],
                'token': [("JSON", "*.json")]
            }
            path = filedialog.askopenfilename(
                title=f"Выберите {file_type} для канала {channel_index + 1}",
                filetypes=filetypes[file_type]
            )
            if path:
                if file_type == 'video':
                    channels[channel_index]['video_path'] = path
                    channels[channel_index]['audio_path'] = ""
                    channels[channel_index]['image_path'] = ""
                elif file_type == 'audio':
                    channels[channel_index]['audio_path'] = path
                elif file_type == 'image':
                    channels[channel_index]['image_path'] = path
                elif file_type == 'token':
                    channels[channel_index]['token_file'] = path
                update_labels(channel_index)

        def update_labels(channel_index):
            """Обновление меток"""
            channel = channels[channel_index]
            content_type = channel['content_type'].get()

            if content_type == 'video':
                label = "Видео не выбрано" if not channel[
                    'video_path'] else f"Видео: {Path(channel['video_path']).name}"
                fg = 'red' if not channel['video_path'] else 'green'
                channel['video_label'].config(text=label, fg=fg)
            elif content_type == 'audio_image':
                audio_label = "Аудио не выбрано" if not channel[
                    'audio_path'] else f"Аудио: {Path(channel['audio_path']).name}"
                image_label = "Изображение не выбрано" if not channel[
                    'image_path'] else f"Изображение: {Path(channel['image_path']).name}"
                channel['audio_label'].config(text=audio_label, fg='green' if channel['audio_path'] else 'red')
                channel['image_label'].config(text=image_label, fg='green' if channel['image_path'] else 'red')

            token_label = "Токен не выбран" if not channel[
                'token_file'] else f"Токен: {Path(channel['token_file']).name}"
            channel['token_label'].config(text=token_label, fg='green' if channel['token_file'] else 'red')

        def start_bot():
            """Запуск загрузки"""
            for i, channel in enumerate(channels):
                content_type = channel['content_type'].get()

                if not channel['token_file']:
                    messagebox.showerror("Ошибка", f"Канал {i + 1}: нет токена!")
                    return

                if content_type == 'video' and not channel['video_path']:
                    messagebox.showerror("Ошибка", f"Канал {i + 1}: нет видео!")
                    return
                elif content_type == 'audio_image' and (not channel['audio_path'] or not channel['image_path']):
                    messagebox.showerror("Ошибка", f"Канал {i + 1}: нет аудио/изображения!")
                    return

            # Подключение VPN/прокси
            for vpn_type, config in vpn_configs:
                connect_to_vpn(vpn_type, config)
            for proxy in proxy_addresses:
                set_proxy(proxy)

            # Обработка каналов
            for i, channel in enumerate(channels):
                try:
                    content_type = channel['content_type'].get()
                    video_path = channel['video_path']

                    if content_type == 'audio_image':
                        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                            temp_path = f.name
                        if not create_video_from_media(channel['image_path'], channel['audio_path'], temp_path):
                            continue
                        video_path = temp_path

                    youtube = authenticate_youtube_account(channel['token_file'])
                    upload_video(
                        youtube,
                        video_path,
                        channel['title_entry'].get(),
                        channel['description_entry'].get(),
                        [tag.strip() for tag in channel['tags_entry'].get().split(",")],
                        channel['publish_at_entry'].get()
                    )

                    if content_type == 'audio_image':
                        os.unlink(video_path)

                except Exception as e:
                    logging.error(f"Ошибка в канале {i + 1}: {str(e)}")
                    messagebox.showerror("Ошибка", f"Канал {i + 1}: {str(e)}")

            cleanup()

        # Кнопка запуска
        tk.Button(scrollable_frame,
                  text="НАЧАТЬ ЗАГРУЗКУ",
                  bg='green',
                  fg='white',
                  font=('Arial', 12, 'bold'),
                  command=start_bot).pack(pady=20)

        root.mainloop()

    except Exception as e:
        logging.error(f"Критическая ошибка: {str(e)}")
        messagebox.showerror("Ошибка", str(e))
        root.destroy()


if __name__ == "__main__":
    gui_interface()