import os
import subprocess
import time
from pathlib import Path


class VPNManager:
    def connect(self):
        """Новая реализация метода"""
        return self.start()  # Используем существующий метод start()

    def start(self):
        # Существующая реализация
        try:
            subprocess.run(
                ["sudo", "openvpn", "--config", str(self.config_path), "--daemon"],
                check=True
            )
            return "✅ VPN успешно запущен"
        except subprocess.CalledProcessError as e:
            return f"❌ Ошибка запуска VPN: {e.stderr.decode()}"

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