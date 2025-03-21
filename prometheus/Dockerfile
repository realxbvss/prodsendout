# Базовый образ Prometheus
FROM prom/prometheus:latest

# Копируем нашу конфигурацию в контейнер
COPY prometheus.yml /etc/prometheus/

# Определяем используемые порты
EXPOSE 9090

# Команда для запуска Prometheus
CMD ["--config.file=/etc/prometheus/prometheus.yml"]