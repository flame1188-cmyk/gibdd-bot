"""
Модуль интеграции с LLM для текстового анализа данных ДТП.

Поддерживает два провайдера:
  1. Бесплатный: ZhipuAI (GLM) — https://open.bigmodel.cn
  2. Платный: любой OpenAI-совместимый агрегатор (AItunnel, OpenRouter и др.)

Функционал:
  1. Генерация аналитического резюме по метрикам ДТП
  2. Ответы на вопросы пользователя по данным
"""

import asyncio
import json
import logging
import time
from typing import Any, Literal

import httpx

from config import (
    LLM_API_KEY, LLM_MODEL,
    LLM_PAID_API_KEY, LLM_PAID_API_URL, LLM_PAID_MODEL,
)

logger = logging.getLogger(__name__)

ZHIPU_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# Тип провайдера: "free" (ZhipuAI/GLM) или "paid" (OpenAI-совместимый)
LLMProvider = Literal["free", "paid"]

# Персистентные HTTP-клиенты (connection pooling)
_free_llm_client: httpx.AsyncClient | None = None
_paid_llm_client: httpx.AsyncClient | None = None


def _get_free_llm_client() -> httpx.AsyncClient:
    """Возвращает переиспользуемый httpx-клиент для ZhipuAI API."""
    global _free_llm_client
    if _free_llm_client is None or _free_llm_client.is_closed:
        _free_llm_client = httpx.AsyncClient(timeout=300)
        logger.info("Создан новый HTTP-клиент для LLM (ZhipuAI)")
    return _free_llm_client


def _get_paid_llm_client() -> httpx.AsyncClient:
    """Возвращает переиспользуемый httpx-клиент для платного провайдера."""
    global _paid_llm_client
    if _paid_llm_client is None or _paid_llm_client.is_closed:
        # 600с = 10 мин: модели с 1M контекстом могут обрабатывать дольше
        _paid_llm_client = httpx.AsyncClient(timeout=600)
        logger.info("Создан новый HTTP-клиент для LLM (платный)")
    return _paid_llm_client


# Для обратной совместимости
_get_llm_client = _get_free_llm_client


async def close_llm_client() -> None:
    """Закрывает все HTTP-клиенты LLM."""
    global _free_llm_client, _paid_llm_client
    for client_var in [_free_llm_client, _paid_llm_client]:
        if client_var is not None and not client_var.is_closed:
            await client_var.aclose()
    _free_llm_client = None
    _paid_llm_client = None


def is_paid_llm_available() -> bool:
    """Проверяет, настроен ли платный LLM-провайдер."""
    return bool(LLM_PAID_API_KEY and LLM_PAID_API_URL)


def is_any_llm_available() -> bool:
    """Проверяет, доступен ли хотя бы один LLM-провайдер."""
    return bool(LLM_API_KEY) or is_paid_llm_available()

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
    "6. Если предоставлены очаги концентрации ДТП — обязательно используй их "
    "в анализе: выдели наиболее опасные участки, оцени тяжесть последствий, "
    "рекомендуй приоритетные мероприятия для конкретных очагов\n"
    "7. Пиши на русском языке, профессиональным но понятным стилем\n"
    "8. Структурируй ответ: выводы, причины, рекомендации\n"
    "9. Если данных недостаточно для вывода — так и скажи\n"
    "10. Не используй эмодзи и markdown-форматирование\n"
    "11. Объём ответа: 3-5 абзацев для резюме, 2-4 абзаца для ответа на вопрос"
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


def format_clusters_for_prompt(
    clusters: list[dict[str, Any]],
    max_clusters: int = 10,
) -> str:
    """
    Форматирует данные об очагах концентрации ДТП для промпта LLM.

    Сортирует очаги по тяжести (погибшие × 3 + раненые × 1 + ДТП),
    выводит топ-N с краткой характеристикой.

    Args:
        clusters: Список словарей очагов (из calculate_concentration_points)
        max_clusters: Максимальное количество очагов для включения

    Returns:
        Текстовый блок для вставки в промпт, или пустую строку если нет очагов
    """
    if not clusters:
        return ""

    # Сортируем по тяжести: погибшие × 3 + раненые × 1 + количество ДТП
    def severity_score(c: dict) -> float:
        return c.get("deaths", 0) * 3 + c.get("injured", 0) * 1 + c.get("total_accidents", 0)

    sorted_clusters = sorted(clusters, key=severity_score, reverse=True)[:max_clusters]

    zone_labels = {
        "settlement_intersection": "Перекрёсток в НП",
        "settlement_road": "Участок дороги в НП (пикетаж)",
        "settlement_segment": "Участок дороги в НП",
        "nonsettlement": "Вне НП",
    }

    lines = []
    lines.append(f"ОЧАГИ КОНЦЕНТРАЦИИ ДТП (всего {len(clusters)}, показаны топ-{len(sorted_clusters)} по тяжести):")
    lines.append("")

    for i, c in enumerate(sorted_clusters, 1):
        zone = zone_labels.get(c["zone_type"], c["zone_type"])
        road = c.get("road", "Не указана")
        total = c["total_accidents"]
        deaths = c.get("deaths", 0)
        injured = c.get("injured", 0)
        dominant = c.get("dominant_type", "")

        # Формируем строку видов ДТП
        type_counter = c.get("type_counter", {})
        types_str = ", ".join(
            f"{t} ({cnt})" for t, cnt in sorted(type_counter.items(), key=lambda x: -x[1])[:3]
        )

        line = (
            f"Очаг {i}: {road} ({zone}) | "
            f"ДТП: {total}, погибло: {deaths}, ранено: {injured}"
        )
        if dominant:
            line += f" | Доминирующий вид: {dominant}"
        lines.append(line)

        if types_str:
            lines.append(f"  Виды ДТП: {types_str}")

        # Пикетаж (если есть)
        start_pos = c.get("start_pos")
        end_pos = c.get("end_pos")
        if start_pos is not None and end_pos is not None:
            lines.append(f"  Пикетаж: {start_pos:.3f} - {end_pos:.3f} км")

        # Даты первого и последнего ДТП
        dates = c.get("dates", [])
        if len(dates) >= 2:
            lines.append(f"  Период: {dates[0]} — {dates[-1]}")

    return "\n".join(lines)


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
    clusters_context: str = "",
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
    if clusters_context:
        prompt += (
            f"\n\n{clusters_context}\n\n"
            f"Обрати особое внимание на очаги ДТП: выдели наиболее опасные участки, "
            f"проанализируй причины концентрации ДТП, предложи конкретные меры "
            f"для каждого из топ-очагов."
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
    clusters_context: str = "",
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
    if clusters_context:
        prompt += f"\n\n{clusters_context}"
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
    provider: LLMProvider = "free",
) -> str:
    """
    Отправляет запрос к LLM и возвращает текстовый ответ.
    Поддерживает два провайдера: бесплатный (ZhipuAI/GLM) и платный
    (OpenAI-совместимый агрегатор, напр. AItunnel).

    При 429 (Too Many Requests) и 5xx (Server Error) автоматически
    повторяет с задержкой.

    Args:
        user_message: Текст запроса пользователя
        system_prompt: Системный промпт (если None — используется стандартный)
        max_retries: Максимальное число повторных попыток при 429/5xx
        provider: "free" (ZhipuAI/GLM) или "paid" (OpenAI-совместимый)

    Returns:
        Текст ответа от модели

    Raises:
        ValueError: если API-ключ не задан
        httpx.HTTPStatusError: при ошибке HTTP (кроме 429 после всех попыток)
    """
    if provider == "paid":
        return await _ask_paid_llm(
            user_message=user_message,
            system_prompt=system_prompt,
            max_retries=max_retries,
        )
    return await _ask_free_llm(
        user_message=user_message,
        system_prompt=system_prompt,
        max_retries=max_retries,
    )


async def _ask_free_llm(
    user_message: str,
    system_prompt: str | None = None,
    max_retries: int = 5,
) -> str:
    """Запрос к бесплатному провайдеру (ZhipuAI / GLM)."""
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
        "max_tokens": 4096,
    }

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    return await _do_llm_request(
        api_url=ZHIPU_API_URL,
        headers=headers,
        payload=payload,
        model_name=LLM_MODEL,
        prompt_len=len(user_message),
        max_retries=max_retries,
        client_getter=_get_free_llm_client,
    )


async def _ask_paid_llm(
    user_message: str,
    system_prompt: str | None = None,
    max_retries: int = 5,
) -> str:
    """Запрос к платному провайдеру (OpenAI-совместимый API)."""
    if not LLM_PAID_API_KEY:
        raise ValueError(
            "LLM_PAID_API_KEY не задан. Добавьте его в .env файл."
        )

    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT

    # Формируем URL (убираем trailing slash, добавляем /chat/completions)
    base_url = LLM_PAID_API_URL.rstrip("/")
    api_url = f"{base_url}/chat/completions"

    payload = {
        "model": LLM_PAID_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.7,
        "max_tokens": 8192,
    }

    headers = {
        "Authorization": f"Bearer {LLM_PAID_API_KEY}",
        "Content-Type": "application/json",
    }

    return await _do_llm_request(
        api_url=api_url,
        headers=headers,
        payload=payload,
        model_name=LLM_PAID_MODEL,
        prompt_len=len(user_message),
        max_retries=max_retries,
        client_getter=_get_paid_llm_client,
    )


async def _do_llm_request(
    api_url: str,
    headers: dict,
    payload: dict,
    model_name: str,
    prompt_len: int,
    max_retries: int,
    client_getter,
) -> str:
    """
    Универсная функция выполнения HTTP-запроса к LLM API.
    Обрабатывает ретраи при 429, 5xx и таймаутах, парсит ответ.
    """
    # --- Глобальный rate limiter ---
    global _last_llm_call_time
    now = time.monotonic()
    elapsed_since_last = now - _last_llm_call_time
    if elapsed_since_last < _MIN_LLM_INTERVAL and _last_llm_call_time > 0:
        cooldown = _MIN_LLM_INTERVAL - elapsed_since_last
        logger.info(f"Rate limiter: ждём {cooldown:.0f} сек между LLM-вызовами...")
        await asyncio.sleep(cooldown)

    logger.info(f"LLM запрос: модель={model_name}, url={api_url}, длина промпта={prompt_len} символов")

    retry_delays = [30, 60, 90, 120, 150]

    for attempt in range(max_retries + 1):
        try:
            client = client_getter()
            response = await client.post(
                api_url,
                headers=headers,
                json=payload,
            )

            if response.status_code == 429:
                if attempt < max_retries:
                    retry_after = (
                        response.headers.get("Retry-After")
                        or response.headers.get("retry-after")
                    )
                    if not retry_after:
                        try:
                            body = response.json()
                            retry_after = str(body.get("retry_after") or body.get("wait") or "")
                        except Exception:
                            pass
                    if retry_after:
                        try:
                            wait = int(float(retry_after)) + 5
                        except (ValueError, TypeError):
                            wait = retry_delays[attempt]
                    else:
                        wait = retry_delays[attempt]

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

            if response.status_code >= 500:
                if attempt < max_retries:
                    wait = retry_delays[attempt]
                    logger.warning(
                        f"LLM {response.status_code} ({response.reason_phrase}). "
                        f"Попытка {attempt + 1}/{max_retries}, "
                        f"ожидание {wait} сек..."
                    )
                    await asyncio.sleep(wait)
                    continue
                response.raise_for_status()

            response.raise_for_status()
            _last_llm_call_time = time.monotonic()
            break

        except httpx.HTTPStatusError:
            raise
        except httpx.TimeoutException:
            if attempt < max_retries:
                wait = retry_delays[min(attempt, len(retry_delays) - 1)]
                logger.warning(
                    f"LLM таймаут. "
                    f"Попытка {attempt + 1}/{max_retries}, "
                    f"ожидание {wait} сек..."
                )
                await asyncio.sleep(wait)
                continue
            raise

    try:
        data = response.json()
    except json.JSONDecodeError as e:
        logger.error(f"LLM вернул невалидный JSON: {e}")
        raise ValueError(f"LLM вернул невалидный ответ") from e

    # Логирование структуры ответа
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"LLM полный ответ: {json.dumps(data, ensure_ascii=False)[:500]}")
    else:
        choices = data.get("choices") or [{}]
        choice = choices[0].get("message", {})
        content_preview = str(choice.get("content", ""))[:100]
        logger.info(
            f"LLM ответ структура: keys={list(data.keys())}, "
            f"finish_reason={choices[0].get('finish_reason')}, "
            f"content_type={type(choice.get('content')).__name__}, "
            f"content_preview={repr(content_preview)}"
        )

    if "choices" not in data or not data["choices"]:
        raise ValueError(f"Неожидаемый ответ API: {json.dumps(data, ensure_ascii=False)[:200]}")

    message = data["choices"][0]["message"]
    content = message.get("content", "") or ""
    reasoning = message.get("reasoning_content", "") or ""

    # Если content пустой — пробуем извлечь ответ из reasoning_content
    if not content and reasoning:
        logger.warning(
            f"LLM вернул пустой content, но есть reasoning_content ({len(reasoning)} симв.). "
            f"Пытаюсь извлечь ответ..."
        )
        paragraphs = [p.strip() for p in reasoning.split("\n") if p.strip()]
        if paragraphs:
            content = "\n".join(paragraphs[-5:]) if len(paragraphs) > 5 else "\n".join(paragraphs)
            logger.info(f"Извлечён ответ из reasoning: {len(content)} симв.")

    if not content:
        msg_keys = list(message.keys())
        logger.warning(
            f"LLM вернул пустой ответ. Ключи message: {msg_keys}, "
            f"finish_reason={data['choices'][0].get('finish_reason')}"
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
    clusters_context: str = "",
    provider: LLMProvider = "free",
) -> str:
    """
    Генерирует аналитическое резюме с помощью LLM.

    Args:
        raw_supplement: Дополнительные данные из сырых карточек ДТП
        news_context: Новостной контекст из открытых источников
        clusters_context: Данные об очагах концентрации ДТП
        provider: "free" (ZhipuAI/GLM) или "paid" (OpenAI-совместимый)

    Returns:
        Текст резюме от нейросети
    """
    prompt = build_summary_prompt(
        comparison, reg_name, current_label, prev_label,
        raw_supplement=raw_supplement,
        news_context=news_context,
        clusters_context=clusters_context,
    )
    return await ask_llm(user_message=prompt, provider=provider)


async def get_ai_answer(
    question: str,
    comparison: dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
    raw_supplement: str = "",
    news_context: str = "",
    clusters_context: str = "",
    provider: LLMProvider = "free",
) -> str:
    """
    Отвечает на вопрос пользователя по данным с помощью LLM.

    Args:
        raw_supplement: Дополнительные данные из сырых карточек ДТП
        news_context: Новостной контекст из открытых источников
        clusters_context: Данные об очагах концентрации ДТП
        provider: "free" (ZhipuAI/GLM) или "paid" (OpenAI-совместимый)

    Returns:
        Текст ответа от нейросети
    """
    prompt = build_question_prompt(
        question, comparison, reg_name, current_label, prev_label,
        raw_supplement=raw_supplement,
        news_context=news_context,
        clusters_context=clusters_context,
    )
    return await ask_llm(user_message=prompt, provider=provider)
