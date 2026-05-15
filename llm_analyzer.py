"""
Модуль интеграции с LLM (GLM) для текстового анализа данных ДТП.

Использует ZhipuAI API (https://open.bigmodel.cn) напрямую через httpx.
Никаких дополнительных зависимостей не требуется.

Функционал:
  1. Генерация аналитического резюме по метрикам ДТП
  2. Ответы на вопросы пользователя по данным
"""

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from config import LLM_API_KEY, LLM_MODEL

logger = logging.getLogger(__name__)

ZHIPU_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# ============================================================
# Глобальный rate limiter — минимальный интервал между ЛЮБЫМИ LLM-вызовами
# ============================================================
_last_llm_call_time: float = 0.0
_MIN_LLM_INTERVAL: float = 5.0  # секунды между запросами (для glm-4.7-flash достаточно)

# ============================================================
# Системный промпт — определяет роль нейросети
# ============================================================

SYSTEM_PROMPT = (
    "Ты — эксперт-аналитик в области безопасности дорожного движения "
    "с 15-летним опытом работы в ГИБДД и МВД России. "
    "Твоя специализация — статистический анализ ДТП, выявление тенденций "
    "и разработка рекомендаций по повышению безопасности.\n\n"
    "Правила:\n"
    "1. Опирайся ТОЛЬКО на предоставленные цифры — не выдумывай данные\n"
    "2. Указывай конкретные цифры и проценты из данных\n"
    "3. Выделяй ключевые тенденции (рост/снижение) и их масштаб\n"
    "4. Предлагай возможные причины выявленных изменений\n"
    "5. Давай конкретные рекомендации по повышению безопасности\n"
    "6. Пиши на русском языке, профессиональным но понятным стилем\n"
    "7. Структурируй ответ: выводы, причины, рекомендации\n"
    "8. Если данных недостаточно для вывода — так и скажи\n"
    "9. Не используй эмодзи и markdown-форматирование\n"
    "10. Объём ответа: 3-5 абзацев для резюме, 2-4 абзаца для ответа на вопрос"
)


# ============================================================
# Форматирование данных для промпта
# ============================================================

def _format_number(val: Any) -> str:
    """Форматирует число с разделителями разрядов."""
    if isinstance(val, float):
        return f"{val:.1f}"
    if isinstance(val, int):
        return f"{val:,}".replace(",", " ")
    return str(val)


def _format_change(change: float) -> str:
    """Форматирует изменение со знаком."""
    if change > 0:
        return f"+{change:.1f}%"
    elif change < 0:
        return f"{change:.1f}%"
    return "0%"


def format_metrics_for_prompt(
    comparison: dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
) -> str:
    """
    Форматирует результаты сравнения в текст для промпта LLM.
    """
    lines = []
    lines.append(f"Регион: {reg_name}")
    lines.append(f"Текущий период: {current_label}")
    lines.append(f"Предыдущий период: {prev_label}")
    lines.append("")

    # Основные показатели
    lines.append("ОСНОВНЫЕ ПОКАЗАТЕЛИ:")

    metrics_info = [
        ("Всего ДТП", comparison["total"]),
        ("Погибло, чел.", comparison["deaths"]),
        ("Ранено, чел.", comparison["injured"]),
        ("ДТП с нетрезвыми водителями", comparison["alcohol"]),
        ("ДТП с пешеходами", comparison["pedestrians"]),
        ("Погибло на 100 ДТП", comparison["deaths_per_100"]),
        ("Ранено на 100 ДТП", comparison["injured_per_100"]),
    ]

    for label, m in metrics_info:
        change = _format_change(m["change"])
        lines.append(
            f"- {label}: {_format_number(m['current'])} "
            f"(было {_format_number(m['previous'])}, изменение {change})"
        )

    lines.append("")

    # По дням недели
    lines.append("РАСПРЕДЕЛЕНИЕ ПО ДНЯМ НЕДЕЛИ:")
    cur_wd = comparison["by_weekday"]["current"]
    prev_wd = comparison["by_weekday"]["previous"]

    day_names = [
        "Понедельник", "Вторник", "Среда", "Четверг",
        "Пятница", "Суббота", "Воскресенье",
    ]

    for day_num in range(7):
        cur = cur_wd.get(day_num, 0)
        prv = prev_wd.get(day_num, 0)
        if prv > 0:
            change = round((cur - prv) / prv * 100, 1)
            lines.append(f"- {day_names[day_num]}: {cur} (было {prv}, {_format_change(change)})")
        else:
            lines.append(f"- {day_names[day_num]}: {cur}")

    lines.append("")

    # По часам
    lines.append("РАСПРЕДЕЛЕНИЕ ПО ЧАСАМ СУТОК (интервалы по 3 часа):")
    cur_hour = comparison["by_hour"]["current"]
    prev_hour = comparison["by_hour"]["previous"]

    for interval_start in range(0, 24, 3):
        interval_end = interval_start + 2
        interval_label = f"{interval_start:02d}:00-{interval_end:02d}:59"
        cur = sum(cur_hour.get(h, 0) for h in range(interval_start, interval_start + 3))
        prv = sum(prev_hour.get(h, 0) for h in range(interval_start, interval_start + 3))
        if prv > 0:
            change = round((cur - prv) / prv * 100, 1)
            lines.append(f"- {interval_label}: {cur} (было {prv}, {_format_change(change)})")
        else:
            lines.append(f"- {interval_label}: {cur}")

    lines.append("")

    # По видам ДТП
    lines.append("РАСПРЕДЕЛЕНИЕ ПО ВИДАМ ДТП:")
    cur_type = comparison["by_type"]["current"]
    prev_type = comparison["by_type"]["previous"]

    all_types = sorted(
        set(list(cur_type.keys()) + list(prev_type.keys())),
        key=lambda x: cur_type.get(x, 0) + prev_type.get(x, 0),
        reverse=True,
    )

    for tp_name in all_types[:10]:
        cur = cur_type.get(tp_name, 0)
        prv = prev_type.get(tp_name, 0)
        if prv > 0:
            change = round((cur - prv) / prv * 100, 1)
            lines.append(f"- {tp_name}: {cur} (было {prv}, {_format_change(change)})")
        else:
            lines.append(f"- {tp_name}: {cur}")

    lines.append("")

    # По погоде
    cur_weather = comparison["by_weather"]["current"]
    prev_weather = comparison["by_weather"]["previous"]

    if cur_weather or prev_weather:
        lines.append("РАСПРЕДЕЛЕНИЕ ПО ПОГОДНЫМ УСЛОВИЯМ:")
        all_w = sorted(
            set(list(cur_weather.keys()) + list(prev_weather.keys())),
            key=lambda x: cur_weather.get(x, 0) + prev_weather.get(x, 0),
            reverse=True,
        )
        for w_name in all_w[:8]:
            cur = cur_weather.get(w_name, 0)
            prv = prev_weather.get(w_name, 0)
            if prv > 0:
                change = round((cur - prv) / prv * 100, 1)
                lines.append(f"- {w_name}: {cur} (было {prv}, {_format_change(change)})")
            else:
                lines.append(f"- {w_name}: {cur}")

    return "\n".join(lines)


# ============================================================
# Построение промптов
# ============================================================

def build_summary_prompt(
    comparison: dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
    raw_supplement: str = "",
    news_context: str = "",
) -> str:
    """Создаёт промпт для генерации аналитического резюме."""
    metrics_text = format_metrics_for_prompt(
        comparison, reg_name, current_label, prev_label,
    )
    prompt = (
        f"{metrics_text}\n\n"
        f"На основе приведённых данных напиши аналитическое резюме:\n"
        f"1. Общая оценка динамики аварийности\n"
        f"2. Ключевые положительные и отрицательные тенденции\n"
        f"3. Возможные причины изменений\n"
        f"4. Рекомендации по повышению безопасности дорожного движения"
    )
    if raw_supplement:
        prompt += f"\n\n{raw_supplement}"
    if news_context:
        prompt += (
            f"\n\n{news_context}\n\n"
            f"Примечание: используй новостной контекст для подтверждения статистических данных, "
            f"упоминания резонансных ДТП и реальных событий. "
            f"Если новость противоречит статистике — укажи на это."
        )
    return prompt


def build_question_prompt(
    question: str,
    comparison: dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
    raw_supplement: str = "",
    news_context: str = "",
) -> str:
    """Создаёт промпт для ответа на вопрос пользователя."""
    metrics_text = format_metrics_for_prompt(
        comparison, reg_name, current_label, prev_label,
    )
    prompt = (
        f"{metrics_text}\n\n"
        f"Вопрос пользователя: {question}\n\n"
        f"Ответь на вопрос, опираясь на приведённые данные. "
        f"Если данных недостаточно — так и скажи."
    )
    if raw_supplement:
        prompt += f"\n\n{raw_supplement}"
    if news_context:
        prompt += f"\n\n{news_context}"
    return prompt


# ============================================================
# Вызов LLM API
# ============================================================

async def ask_llm(
    user_message: str,
    system_prompt: str | None = None,
    max_retries: int = 5,
) -> str:
    """
    Отправляет запрос к GLM API и возвращает текстовый ответ.
    При 429 (Too Many Requests) автоматически повторяет с задержкой.

    Args:
        user_message: Текст запроса пользователя
        system_prompt: Системный промпт (если None — используется стандартный)
        max_retries: Максимальное число повторных попыток при 429

    Returns:
        Текст ответа от модели

    Raises:
        ValueError: если LLM_API_KEY не задан
        httpx.HTTPStatusError: при ошибке HTTP (кроме 429 после всех попыток)
    """
    if not LLM_API_KEY:
        raise ValueError(
            "LLM_API_KEY не задан. Добавьте его в .env файл. "
            "Получить ключ: https://open.bigmodel.cn"
        )

    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.7,
        "max_tokens": 4000,
    }

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    # --- Глобальный rate limiter: ждём, если с предыдущего вызова прошло мало времени ---
    global _last_llm_call_time
    now = time.monotonic()
    elapsed_since_last = now - _last_llm_call_time
    if elapsed_since_last < _MIN_LLM_INTERVAL and _last_llm_call_time > 0:
        cooldown = _MIN_LLM_INTERVAL - elapsed_since_last
        logger.info(f"Rate limiter: ждём {cooldown:.0f} сек между LLM-вызовами...")
        await asyncio.sleep(cooldown)

    logger.info(f"LLM запрос: модель={LLM_MODEL}, длина промпта={len(user_message)} символов")

    # Фоллбэк-задержки при 429 (если нет заголовка Retry-After)
    retry_delays = [30, 60, 90, 120, 150]

    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(
                    ZHIPU_API_URL,
                    headers=headers,
                    json=payload,
                )

            if response.status_code == 429:
                if attempt < max_retries:
                    # Пытаемся прочитать точное время ожидания из заголовка
                    retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
                    if retry_after:
                        try:
                            wait = int(retry_after) + 5  # +5 сек запас
                        except ValueError:
                            wait = retry_delays[attempt]
                    else:
                        wait = retry_delays[attempt]

                    # Минимум 30 сек, даже если Retry-After маленький
                    wait = max(wait, 30)

                    logger.warning(
                        f"LLM 429 Too Many Requests. "
                        f"Попытка {attempt + 1}/{max_retries}, "
                        f"ожидание {wait} сек..."
                        + (f" (Retry-After: {retry_after})" if retry_after else "")
                    )
                    await asyncio.sleep(wait)
                    continue
                else:
                    raise httpx.HTTPStatusError(
                        "Превышен лимит запросов к API. Подождите 5 минут и попробуйте снова.",
                        request=response.request,
                        response=response,
                    )

            response.raise_for_status()

            # Успешный запрос — обновляем время последнего вызова
            _last_llm_call_time = time.monotonic()

        except httpx.HTTPStatusError:
            raise
        except httpx.TimeoutException:
            if attempt < max_retries:
                wait = 20
                logger.warning(f"LLM таймаут. Попытка {attempt + 1}, ожидание {wait} сек...")
                await asyncio.sleep(wait)
                continue
            raise

    data = response.json()

    # Диагностика: логируем структуру ответа (без полного content для экономии)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"LLM полный ответ: {json.dumps(data, ensure_ascii=False)[:500]}")
    else:
        # Даже на INFO логируем ключи и структуру choices
        choice = data.get("choices", [{}])[0].get("message", {})
        content_preview = str(choice.get("content", ""))[:100]
        logger.info(
            f"LLM ответ структура: keys={list(data.keys())}, "
            f"finish_reason={data.get('choices', [{}])[0].get('finish_reason')}, "
            f"content_type={type(choice.get('content')).__name__}, "
            f"content_preview={repr(content_preview)}"
        )

    if "choices" not in data or not data["choices"]:
        raise ValueError(f"Неожидаемый ответ API: {json.dumps(data, ensure_ascii=False)[:200]}")

    content = data["choices"][0]["message"].get("content", "") or ""

    # Если content пустой — логируем полный message для диагностики
    if not content:
        msg_keys = list(data["choices"][0]["message"].keys())
        logger.warning(
            f"LLM вернул пустой content. Ключи message: {msg_keys}, "
            f"полный ответ: {json.dumps(data, ensure_ascii=False)[:500]}"
        )
        raise ValueError("LLM вернул пустой ответ (content='')")

    tokens_used = data.get("usage", {}).get("total_tokens", "?")
    logger.info(f"LLM ответ: {len(content)} символов, токенов: {tokens_used}")

    return content


async def get_ai_summary(
    comparison: dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
    raw_supplement: str = "",
    news_context: str = "",
) -> str:
    """
    Генерирует аналитическое резюме с помощью LLM.

    Args:
        raw_supplement: Дополнительные данные из сырых карточек ДТП
        news_context: Новостной контекст из открытых источников

    Returns:
        Текст резюме от нейросети
    """
    prompt = build_summary_prompt(
        comparison, reg_name, current_label, prev_label,
        raw_supplement=raw_supplement,
        news_context=news_context,
    )
    return await ask_llm(user_message=prompt)


async def get_ai_answer(
    question: str,
    comparison: dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
    raw_supplement: str = "",
    news_context: str = "",
) -> str:
    """
    Отвечает на вопрос пользователя по данным с помощью LLM.

    Args:
        raw_supplement: Дополнительные данные из сырых карточек ДТП
        news_context: Новостной контекст из открытых источников

    Returns:
        Текст ответа от нейросети
    """
    prompt = build_question_prompt(
        question, comparison, reg_name, current_label, prev_label,
        raw_supplement=raw_supplement,
        news_context=news_context,
    )
    return await ask_llm(user_message=prompt)
