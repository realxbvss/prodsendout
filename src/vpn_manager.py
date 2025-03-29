import os
import subprocess
import time
from pathlib import Path


class VPNManager:
    def __init__(self):
        self.config_path = Path(__file__).parent.parent / 'configs' / 'vpn.ovpn'
        self.process = None

    def start(self):
        """Запуск VPN с проверкой статуса"""
        try:
            if self.is_active():
                return "✅ VPN уже активен"

            self.process = subprocess.Popen(
                ["sudo", "openvpn", "--config", str(self.config_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            time.sleep(8)  # Ожидание инициализации
            return "✅ VPN успешно запущен" if self.is_active() else "❌ Не удалось запустить VPN"

        except Exception as e:
            return f"❌ Ошибка запуска VPN: {str(e)}"

    def stop(self):
        """Остановка VPN"""
        try:
            if self.process:
                self.process.terminate()
                self.process.wait()
            subprocess.run(["sudo", "pkill", "openvpn"], check=True)
            return "🛑 VPN остановлен"
        except Exception as e:
            return f"❌ Ошибка остановки VPN: {str(e)}"

    def is_active(self):
        """Проверка активности VPN через ping"""
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "instagram.com"],
                capture_output=True,
                text=True
            )
            return result.returncode == 0
        except:
            return False

    def restart(self):
        """Перезапуск VPN"""
        self.stop()
        return self.start()