"""
Модуль для обработки данных через LLM (ZhipuAI / GLM).
Получает сырые данные, отправляет их в модель вместе с промптом,
возвращает структурированный результат для генерации Excel.
"""

import json
import logging
from typing import Any

from zhipuai import ZhipuAI

from config import ZHIPUAI_API_KEY, LLM_MODEL, LLM_TEMPERATURE, LLM_MAX_TOKENS
from prompts import build_system_prompt, build_user_prompt

logger = logging.getLogger(__name__)

# Инициализация клиента ZhipuAI
_client: ZhipuAI | None = None


def _get_client() -> ZhipuAI:
    """Ленивая инициализация клиента ZhipuAI."""
    global _client
    if _client is None:
        _client = ZhipuAI(api_key=ZHIPUAI_API_KEY)
    return _client


def process_data(
    raw_data: Any,
    user_request: str,
) -> str:
    """
    Обрабатывает сырые данные через GLM и возвращает результат
    в формате CSV (для последующей конвертации в Excel).

    Args:
        raw_data: Сырые данные, полученные из API (dict, list и т.д.)
        user_request: Исходный текстовый запрос пользователя из Telegram

    Returns:
        Строка в CSV-формате (первая строка — заголовки)

    Raises:
        Exception: при ошибке обращения к API ZhipuAI
    """
    client = _get_client()

    # Сериализуем данные для включения в промпт
    if isinstance(raw_data, (dict, list)):
        data_text = json.dumps(raw_data, ensure_ascii=False, indent=2)
    else:
        data_text = str(raw_data)

    # Обрезаем слишком большие данные, чтобы не превысить лимит токенов
    # (примерно ~60 000 символов — безопасный лимит)
    max_data_length = 60000
    if len(data_text) > max_data_length:
        data_text = data_text[:max_data_length] + "\n\n[... ДАННЫЕ ОБРЕЗАНЫ ИЗ-ЗА РАЗМЕРА ...]"
        logger.warning("Данные обрезаны из-за превышения лимита символов")

    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(user_request=user_request, data_text=data_text)

    logger.info(f"Отправка запроса в {LLM_MODEL} (prompt: {len(user_prompt)} символов)")

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=LLM_TEMPERATURE,
        max_tokens=LLM_MAX_TOKENS,
    )

    result = response.choices[0].message.content
    tokens_used = response.usage.total_tokens if response.usage else "N/A"
    logger.info(f"Ответ от {LLM_MODEL} получен ({tokens_used} токенов)")

    if not result:
        raise ValueError("LLM вернула пустой ответ. Проверьте промпт или попробуйте снова.")

    return result
