"""
Конфигурация проекта Telegram-бота для выгрузки данных ДТП с stat.gibdd.ru.
Все ключи и настройки читаются из переменных окружения или файла .env
"""

import os
from dotenv import load_dotenv

# Загружаем переменные из файла .env (если он существует)
load_dotenv()


# ========================
# Telegram Bot
# ========================
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ID пользователей, которым разрешено использовать бота (через запятую)
# Оставьте пустым, чтобы разрешить всем
ALLOWED_USER_IDS: list[int] = []
_raw_allowed = os.getenv("ALLOWED_USER_IDS", "")
if _raw_allowed:
    ALLOWED_USER_IDS = [int(uid.strip()) for uid in _raw_allowed.split(",")]


# ========================
# Сеть
# ========================
# Таймаут запросов к API stat.gibdd.ru (в секундах).
# API ГИБДД может отвечать медленно при больших выборках, ставьте 60-120.
TARGET_API_TIMEOUT: int = int(os.getenv("TARGET_API_TIMEOUT", "120"))

# Прокси (если нужен для корпоративной сети)
HTTP_PROXY: str = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY: str = os.getenv("HTTPS_PROXY", "")


# ========================
# LLM (нейросеть для анализа)
# ========================
# API-ключ для ZhipuAI (GLM). Получить: https://open.bigmodel.cn
# Если не задан — функция "Анализ с ИИ" будет недоступна
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")

# Модель LLM (по умолчанию glm-5v-turbo)
LLM_MODEL: str = os.getenv("LLM_MODEL", "glm-5v-turbo")


# ========================
# Логирование
# ========================
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


# ========================
# Валидация
# ========================
def validate_config() -> list[str]:
    """Проверяет, что все обязательные настройки заданы. Возвращает список ошибок."""
    errors = []

    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN не задан. Получите его у @BotFather в Telegram.")

    return errors
