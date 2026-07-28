"""
Модуль интеграции с LLM для текстового анализа данных ДТП.

Поддерживает два провайдера:
  1. Бесплатный: ZhipuAI (GLM) — https://open.bigmodel.cn
     - Агрегированные метрики + выборка сырых карточек
  2. Платный: любой OpenAI-совместимый агрегатор (AItunnel, OpenRouter и др.)
     - Полные данные участников (Файл 2) в CSV-формате

Функционал:
  1. Генерация аналитического резюме по метрикам ДТП
  2. Ответы на вопросы пользователя по данным
"""

import asyncio
import csv
import io
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

# ============================================================
# Двухуровневый формат данных для полного промпта платного метода
# ============================================================
# Максимальный размер данных для LLM (символов, на ОДИН период).
# DeepSeek V4 Flash: 1M токенов ≈ ~3-4M символов.
# Мы отправляем полные данные только за текущий период,
# предыдущий покрывается агрегированными метриками.
# Лимит оставляет место для: системный промпт + метрики + очаги + новости + ответ.
_FULL_DATA_MAX_CHARS = 3_500_000  # ~3.5 млн символов (DeepSeek V4 Flash: 1M токенов ≈ 3-4M симв., сэмплинг отключён)

# --- Столбцы уровня ДТП (печатается 1 раз на ДТП) ---
_DTP_LEVEL_COLUMNS = [
    "Дата",
    "Время",
    "Вид ДТП",
    "Место",
    "Населенный пункт",
    "Улица",
    "Дорога",
    "Километр",
    "Метр",
    "Погибло",
    "Ранено",
    "НДУ",
    "Объекты УДС на месте",
    "Факторы, влияющие на режим движения",
    "Состояние проезжей части",
    "Состояние погоды",
    "Освещение",
    "Значение дороги",
]

# --- Столбцы уровня участника (печатается для каждого участника) ---
_PARTICIPANT_LEVEL_COLUMNS = [
    "Тип ТС",
    "Категория",
    "Пол",
    "Тяжесть последствий",
    "Непосредственные нарушения ПДД",
    "Сопутствующие нарушения ПДД",
    "Стаж(лет)",
    "Результат МО",
    "Пристёгнут",
]

# Значения, которые считаются «шумом» и заменяются на пустую строку.
# Логика: отсутствие записи = отсутствие дефекта/нарушения.
_NOISE_VALUES = frozenset({
    # Недостатки УДС
    "Не установлены",
    # Факторы режима движения
    "Сведения отсутствуют",
    # Нарушения ПДД
    "Нет нарушений",
})


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
    "Тебе предоставлены: агрегированные метрики (основные показатели, "
    "распределения по дням, часам, видам ДТП, погоде) и кросс-таблицы — "
    "перекрёстные распределения, показывающие связи между факторами.\n\n"
    "Кросс-таблицы позволяют выявлять корреляции, которые не видны "
    "в плоских агрегатах: например, как время суток влияет на тяжесть последствий, "
    "какой стаж водителей наиболее опасен, как значение дороги (федеральная/региональная/муниципальная) "
    "коррелирует с аварийностью.\n\n"
    "Правила:\n"
    "1. Опирайся ТОЛЬКО на предоставленные цифры — не выдумывай данные\n"
    "2. Указывай конкретные цифры и проценты из данных\n"
    "3. Выделяй ключевые тенденции (рост/снижение) и их масштаб\n"
    "4. Используй кросс-таблицы для выявления корреляций между факторами\n"
    "5. Предлагай возможные причины выявленных изменений\n"
    "6. Давай конкретные рекомендации по повышению безопасности\n"
    "7. Если предоставлены очаги концентрации ДТП — обязательно используй их "
    "в анализе: выдели наиболее опасные участки, оцени тяжесть последствий, "
    "рекомендуй приоритетные мероприятия для конкретных очагов\n"
    "8. Пиши на русском языке, профессиональным но понятным стилем\n"
    "9. Структурируй ответ: выводы, причины, рекомендации\n"
    "10. Если данных недостаточно для вывода — так и скажи\n"
    "11. Не используй эмодзи и markdown-форматирование\n"
    "12. Объём ответа: 3-5 абзацев для резюме, 2-4 абзаца для ответа на вопрос"
)


# ============================================================
# Расширенный системный промпт для платного метода (полные данные)
# ============================================================

SYSTEM_PROMPT_PAID = (
    "Ты — старший эксперт-аналитик в области безопасности дорожного движения "
    "с 15-летним опытом работы в ГИБДД, МВД России и научно-исследовательских "
    "центрах БДД. Ты специализируешься на глубинном статистическом анализе ДТП, "
    "выявлении скрытых корреляций, паттернов и аномалий.\n\n"
    "Тебе предоставлены полные данные по каждому участнику ДТП за оба периода "
    "(текущий и предыдущий) в двухуровневом формате, а также агрегированная "
    "сводная статистика.\n\n"
    "Формат данных — двухуровневый:\n"
    "  [ДТП] — данные о ДТП (печатается 1 раз): дата, время, вид, место, дорога, "
    "погибло, ранено, дорожные условия, погода и т.д.\n"
    "  [Уч.N] — данные об участнике (каждый отдельной строкой): тип ТС, категория, "
    "пол, тяжесть последствий, нарушения ПДД, стаж, опьянение, ремень безопасности.\n"
    "Одно ДТП = одна строка [ДТП] + одна или несколько строк [Уч.N].\n"
    "Пустое поле означает отсутствие данных (нет недостатков УДС, нет нарушений и т.д.).\n\n"
    "КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:\n"
    "1. Опирайся ТОЛЬКО на предоставленные данные — категорически запрещено "
    "выдумывать, угадывать или предполагать цифры и проценты, которых нет в данных.\n"
    "2. Каждое числовое утверждение (процент, доля, «около X%», «более X%») "
    "должно быть подкреплено конкретным значением из данных. Если ты не можешь "
    "точно посчитать — не пиши число, пиши качественно.\n"
    "3. Не повторяй одну и ту же информацию в разных разделах. "
    "Каждый раздел должен содержать уникальную информацию. "
    "Каждая тема (например, 'пешеходы', 'алкоголь', 'скорость') "
    "должна упоминаться не более чем в 2 разделах.\n"
    "4. Структурируй ответ по разделам с заголовками (без markdown, без эмодзи).\n"
    "5. Пиши на русском языке, профессиональным но понятным стилем.\n"
    "6. Не дублируй раздел «Очаги ДТП» — упомяни их один раз в соответствующем разделе.\n\n"
    "Анализ должен включать:\n"
    "1. ОБЩАЯ ОЦЕНКА ДИНАМИКИ — сравнение текущего и предыдущего периода, "
    "рост/снижение ключевых показателей. Только реальные цифры из агрегированных метрик.\n"
    "2. КОРРЕЛЯЦИИ — взаимосвязи между факторами, подтверждённые данными:\n"
    "   - Время суток и тяжесть последствий\n"
    "   - Стаж водителя и нарушения ПДД\n"
    "   - Погодные условия и виды ДТП\n"
    "   - Тип транспортного средства и тяжесть\n"
    "   - Освещение и вид ДТП\n"
    "   - Значение дороги (федеральные, региональные, муниципальные) и "
    "доля ДТП/погибших/раненых по каждой категории\n"
    "   - Опьянение и время суток/день недели\n"
    "   - Использование ремня безопасности и тяжесть последствий\n"
    "   Для каждой корреляции приведи конкретные числа из данных.\n"
    "3. ПРОФИЛИРОВАНИЕ УЧАСТНИКОВ — обязательно приведи конкретные данные:\n"
    "   - Топ-5 нарушений ПДД с количеством случаев за каждый период\n"
    "   - Распределение по стажу водителей (группы: 0-2, 3-5, 6-10, 10+ лет) "
    "с привязкой к тяжести последствий\n"
    "   - Доли категорий участников (водители/пешеходы/пассажиры) с числами\n"
    "   - Доля нетрезвых водителей с числом случаев\n"
    "4. ПАТТЕРНЫ И АНОМАЛИИ — типичные комбинации факторов в ДТП, "
    "нетипичные случаи, сезонные и временные закономерности. "
    "ПРАВИЛО РЕПРЕЗЕНТАТИВНОСТИ: если ты приводишь конкретный пример ДТП "
    "для иллюстрации паттерна, он должен быть репрезентативным. "
    "Не обобщай по единичному случаю. Для вывода нужно не менее 3 "
    "подтверждающих примеров из данных. Если примеров меньше — "
    "сформулируй вывод как предположение (возможно, может указывать).\n"
    "5. ПРОГНОЗ РИСКОВ — где вероятен рост аварийности, какие факторы "
    "усиливают риск, наиболее опасные локации. Только на основе выявленных паттернов.\n"
    "6. РЕКОМЕНДАЦИИ — НЕ БОЛЕЕ 5 конкретных мер. Каждая рекомендация должна содержать:\n"
    "   - Конкретную проблему (из данных)\n"
    "   - Конкретную меру (не общие слова типа «усилить контроль»). "
    "Категорически запрещено указывать конкретные модели, бренды, "
    "ГОСТы или артикулы оборудования (не пиши «Т7», «ЮМЗ», «ГОСТ 50597-2017» и т.п.). "
    "Пиши тип оборудования обобщённо: светофор с кнопкой вызова, "
    "тросовое ограждение, камера фотовидеофиксации, лежачий полицейский и т.д.\n"
    "   - Конкретную локацию (дорогу, перекрёсток, населённый пункт из данных)\n"
    "   Качество важнее количества. Никаких шаблонных фраз.\n\n"
    "7. Если предоставлены очаги концентрации ДТП — проанализируй их один раз: "
    "оцени наиболее опасные участки, причины концентрации, "
    "рекомендуй конкретные мероприятия для каждого топ-очага.\n\n"
    "8. Если предоставлен новостной контекст — используй для подтверждения "
    "статистики и упоминания реальных событий. "
    "Если новость противоречит статистике — укажи на это.\n\n"
    "Объём ответа: развёрнутый анализ, каждый раздел — с конкретными цифрами из данных. "
    "Приоритет — точность и конкретика, а не объём."
)


# ============================================================
# Форматирование полных данных для платного промпта
# ============================================================

def _clean_noise(value: str) -> str:
    """Заменяет шумовые значения ("Не установлены", "Сведения отсутствуют",
    "Нет нарушений") на пустую строку для сокращения промпта."""
    v = value.strip()
    return "" if v in _NOISE_VALUES else v


def _format_dtp_block(
    dtp_fields: list[str],
    dtp_col_names: list[str],
) -> str:
    """Формирует одну строку уровня ДТП: [ДТП] Дата; Время; Вид ДТП; ...
    Замыкающие пустые поля обрезаются для экономии."""
    cleaned = [_clean_noise(v) for v in dtp_fields]
    # Обрезаем замыкающие пустые поля
    while cleaned and cleaned[-1].strip() == "":
        cleaned.pop()
    return "[ДТП] " + "; ".join(cleaned)


def _format_uch_block(
    uch_fields: list[str],
    uch_col_names: list[str],
    participant_num: int,
) -> str:
    """Формирует одну строку уровня участника: [Уч.N] Тип ТС; Категория; ...
    Замыкающие пустые поля обрезаются для экономии."""
    cleaned = [_clean_noise(v) for v in uch_fields]
    # Обрезаем замыкающие пустые поля
    while cleaned and cleaned[-1].strip() == "":
        cleaned.pop()
    return f"[Уч.{participant_num}] " + "; ".join(cleaned)


def format_full_data_as_csv(
    cards: list[dict[str, Any]],
    label: str,
) -> str:
    """
    Формирует полные данные участников (Файл 2) в двухуровневом формате
    для передачи в LLM в платном методе.

    Формат:
      [ДТП] Дата; Время; Вид ДТП; Место; ...  (столбцы уровня ДТП, 1 раз)
      [Уч.1] Тип ТС; Категория; Пол; ...      (столбцы уровня участника)
      [Уч.2] Тип ТС; Категория; Пол; ...
      [ДТП] ...

    Преимущества перед плоским CSV:
      - DTP-поля не дублируются для каждого участника (экономия ~40-50%)
      - Структура «одно ДТП → несколько участников» очевидна для LLM

    Очистка шумовых значений:
      - «Не установлены» (НДУ) → пусто
      - «Сведения отсутствуют» (Факторы) → пусто
      - «Нет нарушений» (ПДД) → пусто

    Сэмплинг при превышении лимита:
      - Приоритетные ДТП (смертельные/алкогольные/пешеходные) целиком
      - Остальные — случайная выборка до лимита

    Args:
        cards: Список сырых карточек ДТП
        label: Подпись периода (например "I полугодие 2026")

    Returns:
        Текст в двухуровневом формате для вставки в промпт
    """
    if not cards:
        return f"\nПОЛНЫЕ ДАННЫЕ УЧАСТНИКОВ ({label}): нет данных\n"

    # Импортируем здесь, чтобы избежать циклического импорта на уровне модуля
    from gibdd_parser import build_file2_data

    # Строим полный Файл 2 (все строки, все столбцы)
    all_rows = build_file2_data(cards)
    all_columns = list(all_rows[0].keys()) if all_rows else []

    # Предваряем вычисляем индексы для столбцов обоих уровней
    dtp_indices = []
    for col_name in _DTP_LEVEL_COLUMNS:
        if col_name in all_columns:
            dtp_indices.append(all_columns.index(col_name))
        else:
            dtp_indices.append(None)

    uch_indices = []
    for col_name in _PARTICIPANT_LEVEL_COLUMNS:
        if col_name in all_columns:
            uch_indices.append(all_columns.index(col_name))
        else:
            uch_indices.append(None)

    # === Этап 1: Группируем строки по ДТП ===
    # Ключ ДТП: Дата + Время + Вид ДТП (эти 3 поля уникально идентифицируют ДТП)
    def _extract_values(row_dict: dict, indices: list[int | None]) -> list[str]:
        values = list(row_dict.values())
        return [str(values[i]).strip() if i is not None else "" for i in indices]

    # Группируем: список из (dtp_fields, [uch_fields], dtp_key, card_idx)
    dtp_groups: list[dict] = []
    dtp_key_to_group: dict[tuple, int] = {}

    for row_idx, row in enumerate(all_rows):
        row_values = list(row.values())
        # Ключ из Дата + Время + Вид ДТП
        dtp_key = (row_values[2], row_values[3], row_values[4]) if len(row_values) > 4 else ("")

        if dtp_key in dtp_key_to_group:
            group_idx = dtp_key_to_group[dtp_key]
            uch_fields = _extract_values(row, uch_indices)
            dtp_groups[group_idx]["participants"].append(uch_fields)
        else:
            dtp_fields = _extract_values(row, dtp_indices)
            uch_fields = _extract_values(row, uch_indices)
            group_idx = len(dtp_groups)
            dtp_key_to_group[dtp_key] = group_idx
            dtp_groups.append({
                "dtp_key": dtp_key,
                "dtp_fields": dtp_fields,
                "participants": [uch_fields],
            })

    # === Этап 2: Размечаем приоритетные ДТП ===
    # Индексы внутри _DTP_LEVEL_COLUMNS:
    #   Погибло = index 11 ("Погибло" в _DTP_LEVEL_COLUMNS)
    #   Ранено  = index 12 ("Ранено" в _DTP_LEVEL_COLUMNS)
    # Индексы внутри _PARTICIPANT_LEVEL_COLUMNS:
    #   Результат МО = index 7 ("Результат МО" в _PARTICIPANT_LEVEL_COLUMNS)
    #   Категория   = index 1 ("Категория" в _PARTICIPANT_LEVEL_COLUMNS)
    POG_IDX = _DTP_LEVEL_COLUMNS.index("Погибло")
    RAN_IDX = _DTP_LEVEL_COLUMNS.index("Ранено")
    MO_IDX = _PARTICIPANT_LEVEL_COLUMNS.index("Результат МО")
    CAT_IDX = _PARTICIPANT_LEVEL_COLUMNS.index("Категория")

    priority_indices: set[int] = set()
    for g_idx, group in enumerate(dtp_groups):
        pog = group["dtp_fields"][POG_IDX]
        # Смертельные ДТП
        if pog and pog not in ("", "0"):
            priority_indices.add(g_idx)
            continue
        # ДТП с нетрезвыми или пешеходами — проверяем участников
        for uch in group["participants"]:
            result_mo = uch[MO_IDX]
            category = uch[CAT_IDX]
            if result_mo and result_mo.lower() == "да":
                priority_indices.add(g_idx)
                break
            if category and "пешеход" in category.lower():
                priority_indices.add(g_idx)
                break

    # === Этап 3: Формируем текстовый блок ===
    def _group_to_lines(group: dict) -> list[str]:
        lines = [_format_dtp_block(group["dtp_fields"], _DTP_LEVEL_COLUMNS)]
        for p_idx, uch in enumerate(group["participants"], 1):
            lines.append(_format_uch_block(uch, _PARTICIPANT_LEVEL_COLUMNS, p_idx))
        return lines

    # Считаем общий размер
    header = f"Формат: [ДТП] = данные ДТП; [Уч.N] = данные участника N\n"
    header += f"Столбцы ДТП: {'; '.join(_DTP_LEVEL_COLUMNS)}\n"
    header += f"Столбцы участников: {'; '.join(_PARTICIPANT_LEVEL_COLUMNS)}\n"

    all_lines = []
    total_participants = 0
    for group in dtp_groups:
        all_lines.extend(_group_to_lines(group))
        total_participants += len(group["participants"])

    full_text = header + "\n".join(all_lines)
    total_chars = len(full_text)

    # Если вписываемся в лимит — отдаём всё
    if total_chars <= _FULL_DATA_MAX_CHARS:
        logger.info(
            f"Полные данные для LLM ({label}): "
            f"{len(dtp_groups)} ДТП, {total_participants} участников, "
            f"{total_chars} символов (двухуровневый формат)"
        )
        return f"\nПОЛНЫЕ ДАННЫЕ УЧАСТНИКОВ ({label}, {len(dtp_groups)} ДТП, {total_participants} участников):\n{full_text}"

    # === Этап 4: Сэмплинг ===
    import random
    random.seed(42)  # воспроизводимость

    logger.info(
        f"Полные данные для LLM ({label}): {total_chars} символов — "
        f"превышает лимит {_FULL_DATA_MAX_CHARS}, применяем сэмплинг"
    )

    # Разделяем на приоритетные и остальные
    priority_groups = [g for i, g in enumerate(dtp_groups) if i in priority_indices]
    other_groups = [g for i, g in enumerate(dtp_groups) if i not in priority_indices]

    # Считаем размер приоритетных (включая заголовок)
    priority_lines = []
    priority_participants = 0
    for group in priority_groups:
        priority_lines.extend(_group_to_lines(group))
        priority_participants += len(group["participants"])
    priority_text = header + "\n".join(priority_lines)
    remaining_chars = _FULL_DATA_MAX_CHARS - len(header) - 100  # запас

    if len(priority_text) > _FULL_DATA_MAX_CHARS:
        # Даже приоритетные не вмещаются — берём только их
        logger.warning(
            f"Полные данные ({label}): приоритетные ДТП "
            f"({len(priority_groups)}) не вмещаются, обрезаем"
        )
        lines = priority_text.split("\n")
        result_lines = [lines[0]]  # заголовок
        current_size = len(lines[0])
        for line in lines[1:]:
            if current_size + len(line) + 1 > _FULL_DATA_MAX_CHARS:
                break
            result_lines.append(line)
            current_size += len(line) + 1
        full_text = "\n".join(result_lines)
        return f"\nПОЛНЫЕ ДАННЫЕ УЧАСТНИКОВ ({label}, сэмплинг, {len(priority_groups)} ДТП):\n{full_text}"

    # Добавляем случайные ДТП из остальных до лимита
    space_left = remaining_chars - len(priority_text)
    # Средний размер одной группы ДТП (в символах)
    avg_group_chars = sum(
        len("\n".join(_group_to_lines(g))) + 1
        for g in other_groups[:100]
    ) / min(len(other_groups), 100) if other_groups else 200
    sample_size = max(1, int(space_left / max(avg_group_chars, 1)))
    sampled_others = random.sample(other_groups, min(sample_size, len(other_groups))) if other_groups else []

    # Объединяем: приоритетные + случайные, сортируем по дате/времени
    all_sampled = priority_groups + sampled_others
    all_sampled.sort(key=lambda g: (g["dtp_fields"][0], g["dtp_fields"][1]))  # Дата, Время

    result_lines = []
    sampled_participants = 0
    for group in all_sampled:
        result_lines.extend(_group_to_lines(group))
        sampled_participants += len(group["participants"])
    full_text = header + "\n".join(result_lines)

    logger.info(
        f"Полные данные ({label}): сэмплинг — "
        f"приоритетных ДТП {len(priority_groups)}, "
        f"случайных ДТП {len(sampled_others)}, "
        f"итого {len(all_sampled)} ДТП, {sampled_participants} участников, "
        f"{len(full_text)} символов"
    )
    return f"\nПОЛНЫЕ ДАННЫЕ УЧАСТНИКОВ ({label}, сэмплинг, {len(all_sampled)} ДТП, {sampled_participants} участников):\n{full_text}"


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


def format_cross_tables_for_prompt(
    current_cross: dict[str, Any],
    prev_cross: dict[str, Any] | None = None,
    current_label: str = "текущий",
    prev_label: str = "предыдущий",
) -> str:
    """
    Форматирует кросс-таблицы в компактный текст для промпта LLM.

    Каждая таблица — это компактный блок вида:
      НАЗВАНИЕ:
      строка1: кол1 | кол2 | кол3
      строка2: ...

    Args:
        current_cross: Кросс-таблицы текущего периода (из calculate_cross_tables)
        prev_cross: Кросс-таблицы предыдущего периода (опционально)
        current_label: Подпись текущего периода
        prev_label: Подпись предыдущего периода
    """
    from collections import Counter

    lines = []
    lines.append("КРОСС-ТАБЛИЦЫ:")
    lines.append("")

    def _fmt_severity_table(
        title: str,
        cur_table: dict[str, dict],
        prev_table: dict[str, dict] | None = None,
        sort_key: str = "dtp",
        max_rows: int = 15,
    ) -> list[str]:
        """Форматирует таблицу {key: {dtp, deaths, injured}}."""
        rows = []
        rows.append(f"  {title}:")
        rows.append(f"  {'Категория':<20} | {'ДТП':>5} | {'Погибло':>7} | {'Ранено':>6}")
        if prev_table:
            rows[-1] += f" | {'ДТП было':>8} | {'Измен.':>7}"
        rows.append(f"  {'-'*20}-+-{'-'*5}-+-{'-'*7}-+-{'-'*6}")
        if prev_table:
            rows[-1] += f"-+-{'-'*8}-+-{'-'*7}"

        sorted_keys = sorted(
            cur_table.keys(),
            key=lambda k: cur_table[k].get(sort_key, 0),
            reverse=True,
        )[:max_rows]

        for key in sorted_keys:
            c = cur_table[key]
            dtp_c = c.get("dtp", 0)
            deaths_c = c.get("deaths", 0)
            injured_c = c.get("injured", 0)
            line = f"  {key:<20} | {dtp_c:>5} | {deaths_c:>7} | {injured_c:>6}"

            if prev_table and key in prev_table:
                p = prev_table[key]
                dtp_p = p.get("dtp", 0)
                change = _format_change(round((dtp_c - dtp_p) / dtp_p * 100, 1)) if dtp_p > 0 else "новое"
                line += f" | {dtp_p:>8} | {change:>7}"
            rows.append(line)

        rows.append("")
        return rows

    def _fmt_part_severity_table(
        title: str,
        cur_table: dict[str, dict],
        max_rows: int = 10,
    ) -> list[str]:
        """Форматирует таблицу {key: {participants, deaths, injured, unhurt}}."""
        rows = []
        rows.append(f"  {title}:")
        rows.append(f"  {'Категория':<20} | {'Всего':>5} | {'Погибло':>7} | {'Ранено':>6} | {'Без послед.':>11}")
        rows.append(f"  {'-'*20}-+-{'-'*5}-+-{'-'*7}-+-{'-'*6}-+-{'-'*11}")

        sorted_keys = sorted(
            cur_table.keys(),
            key=lambda k: cur_table[k].get("participants", 0),
            reverse=True,
        )[:max_rows]

        for key in sorted_keys:
            c = cur_table[key]
            total = c.get("participants", 0)
            deaths = c.get("deaths", 0)
            injured = c.get("injured", 0)
            unhurt = c.get("unhurt", 0)
            rows.append(
                f"  {key:<20} | {total:>5} | {deaths:>7} | {injured:>6} | {unhurt:>11}"
            )
        rows.append("")
        return rows

    def _fmt_counter_table(
        title: str,
        cur_counter: dict[str, Counter],
        max_rows: int = 8,
        max_cols: int = 5,
    ) -> list[str]:
        """Форматирует таблицу {key: Counter(values)}."""
        rows = []
        rows.append(f"  {title}:")

        sorted_keys = sorted(
            cur_counter.keys(),
            key=lambda k: sum(cur_counter[k].values()),
            reverse=True,
        )[:max_rows]

        for key in sorted_keys:
            top_items = cur_counter[key].most_common(max_cols)
            items_str = ", ".join(f"{v} ({cnt})" for v, cnt in top_items)
            rows.append(f"    {key}: {items_str}")
        rows.append("")
        return rows

    def _fmt_lighting_ped_table(
        title: str,
        cur_table: dict[str, dict],
    ) -> list[str]:
        """Форматирует таблицу {lighting: {dtp_with_ped, total_dtp}}."""
        rows = []
        rows.append(f"  {title}:")
        rows.append(f"  {'Освещение':<25} | {'Всего ДТП':>9} | {'С пешеходами':>13} | {'Доля, %':>7}")
        rows.append(f"  {'-'*25}-+-{'-'*9}-+-{'-'*13}-+-{'-'*7}")

        sorted_keys = sorted(
            cur_table.keys(),
            key=lambda k: cur_table[k].get("total_dtp", 0),
            reverse=True,
        )

        for key in sorted_keys:
            c = cur_table[key]
            total = c.get("total_dtp", 0)
            ped = c.get("dtp_with_ped", 0)
            share = round(ped / total * 100, 1) if total > 0 else 0
            rows.append(
                f"  {key:<25} | {total:>9} | {ped:>13} | {share:>6.1f}%"
            )
        rows.append("")
        return rows

    def _fmt_alcohol_dist_table(
        title: str,
        cur_counter: dict[str, Counter],
        prev_counter: dict[str, Counter] | None = None,
        label_type: str = "weekday",
    ) -> list[str]:
        """Форматирует распределение опьянения по дням/часам."""
        rows = []
        rows.append(f"  {title}:")

        day_names = {
            0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс",
        }
        alc_data = cur_counter.get("да", Counter())

        if label_type == "weekday":
            # Заголовок
            header = "     "
            for d in range(7):
                header += f" | {day_names[d]:>5}"
            rows.append(header)
            rows.append(f"  Алкоголь{'-' * (len(header) - 10)}")
            vals = "     "
            for d in range(7):
                vals += f" | {alc_data.get(d, 0):>5}"
            rows.append(vals)
        else:
            # По 3-часовым интервалам
            sorted_intervals = sorted(alc_data.keys())
            for interval in sorted_intervals:
                rows.append(f"    {interval}: {alc_data[interval]}")

        rows.append("")
        return rows

    # 1. Час × тяжесть
    prev_h = prev_cross.get("hour_x_severity") if prev_cross else None
    lines.extend(_fmt_severity_table(
        "Время суток × тяжесть", current_cross["hour_x_severity"], prev_h, max_rows=8,
    ))

    # 2. День недели × тяжесть
    prev_w = prev_cross.get("weekday_x_severity") if prev_cross else None
    lines.extend(_fmt_severity_table(
        "День недели × тяжесть", current_cross["weekday_x_severity"], prev_w, max_rows=7,
    ))

    # 3. Стаж × тяжесть (участники)
    lines.extend(_fmt_part_severity_table(
        "Стаж водителя × тяжесть последствий",
        current_cross["experience_x_severity"],
    ))

    # 4. Стаж × топ-нарушения
    lines.extend(_fmt_counter_table(
        "Стаж водителя × типы нарушений ПДД",
        current_cross["experience_x_violations"],
    ))

    # 5. Тип ТС × тяжесть
    lines.extend(_fmt_part_severity_table(
        "Тип транспортного средства × тяжесть последствий",
        current_cross["vehicle_type_x_severity"],
    ))

    # 6. Значение дороги × тяжесть
    prev_rv = prev_cross.get("road_value_x_severity") if prev_cross else None
    lines.extend(_fmt_severity_table(
        "Значение дороги × тяжесть (Федеральные/Региональные/Муниципальные)",
        current_cross["road_value_x_severity"], prev_rv, max_rows=5,
    ))

    # 7. Погода × вид ДТП
    lines.extend(_fmt_counter_table(
        "Погодные условия × вид ДТП",
        current_cross["weather_x_dtp_type"],
    ))

    # 8. Освещение × доля пешеходных ДТП
    lines.extend(_fmt_lighting_ped_table(
        "Освещение × доля ДТП с пешеходами",
        current_cross["lighting_x_pedestrian_share"],
    ))

    # 9. Ремень безопасности × тяжесть
    lines.extend(_fmt_part_severity_table(
        "Ремень безопасности × тяжесть последствий",
        current_cross["belt_x_severity"],
    ))

    # 10. Опьянение × день недели
    prev_aw = prev_cross.get("alcohol_x_weekday") if prev_cross else None
    lines.extend(_fmt_alcohol_dist_table(
        "Опьянение × день недели",
        current_cross["alcohol_x_weekday"], prev_aw, "weekday",
    ))

    # 11. Опьянение × час
    prev_ah = prev_cross.get("alcohol_x_hour") if prev_cross else None
    lines.extend(_fmt_alcohol_dist_table(
        "Опьянение × время суток",
        current_cross["alcohol_x_hour"], prev_ah, "hour",
    ))

    # 12. Пол × тяжесть
    lines.extend(_fmt_part_severity_table(
        "Пол участника × тяжесть последствий",
        current_cross["gender_x_severity"],
    ))

    # 13. Категория участника × тяжесть
    lines.extend(_fmt_part_severity_table(
        "Категория участника × тяжесть последствий",
        current_cross["participant_category_x_severity"],
    ))

    # 14. Вид ДТП × тяжесть
    prev_dt = prev_cross.get("dtp_type_x_severity") if prev_cross else None
    lines.extend(_fmt_severity_table(
        "Вид ДТП × тяжесть",
        current_cross["dtp_type_x_severity"], prev_dt, max_rows=10,
    ))

    # 15. Погода × тяжесть
    prev_ws = prev_cross.get("weather_x_severity") if prev_cross else None
    lines.extend(_fmt_severity_table(
        "Погодные условия × тяжесть",
        current_cross["weather_x_severity"], prev_ws, max_rows=8,
    ))

    # 16. Освещение × тяжесть
    prev_ls = prev_cross.get("lighting_x_severity") if prev_cross else None
    lines.extend(_fmt_severity_table(
        "Освещение × тяжесть",
        current_cross["lighting_x_severity"], prev_ls, max_rows=5,
    ))

    # 17. Месяц × тяжесть
    prev_ms = prev_cross.get("month_x_severity") if prev_cross else None
    lines.extend(_fmt_severity_table(
        "Месяц × тяжесть",
        current_cross["month_x_severity"], prev_ms, max_rows=12, sort_key="deaths",
    ))

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
    cross_tables_context: str = "",
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
    if cross_tables_context:
        prompt += (
            f"\n\n{cross_tables_context}\n\n"
            f"Используй кросс-таблицы для выявления скрытых корреляций: "
            f"как время суток влияет на тяжесть, какой стаж водителей наиболее опасен, "
            f"как погода и освещение коррелируют с видами ДТП, "
            f"как значение дороги (федеральная/региональная/муниципальная) влияет на аварийность."
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


def build_paid_summary_prompt(
    comparison: dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
    current_full_data: str = "",
    prev_full_data: str = "",
    news_context: str = "",
    clusters_context: str = "",
) -> str:
    """
    Создаёт промпт для генерации глубокого аналитического резюме
    в платном методе (полные данные участников).

    Структура промпта:
      1. Агрегированные метрики (сводка за оба периода)
      2. Полные данные участников (текущий период)
      3. Полные данные участников (предыдущий период)
      4. Очаги концентрации ДТП
      5. Новостной контекст
      6. Инструкция по анализу
    """
    # 1. Агрегированные метрики
    metrics_text = format_metrics_for_prompt(
        comparison, reg_name, current_label, prev_label,
    )

    parts = []

    parts.append(
        f"{metrics_text}\n\n"
        f"Выше приведена сводная статистика по двум периодам (текущий и предыдущий). "
        f"Далее следуют полные данные по каждому участнику ДТП "
        f"за оба периода в двухуровневом формате: "
        f"[ДТП] — общие данные ДТП, [Уч.N] — данные участника. "
        f"Сравни оба периода по всем измерениям: нарушения, стаж, типы ТС, погода, время и т.д."
    )

    # 2. Полные данные текущего периода
    if current_full_data:
        parts.append(current_full_data)

    # 3. Полные данные предыдущего периода
    if prev_full_data:
        parts.append(prev_full_data)

    # 4. Очаги концентрации
    if clusters_context:
        parts.append(
            f"\n{clusters_context}\n\n"
            f"Обрати особое внимание на очаги ДТП: выдели наиболее опасные участки, "
            f"проанализируй причины концентрации, предложи конкретные мероприятия "
            f"для каждого из топ-очагов."
        )

    # 5. Новости
    if news_context:
        parts.append(
            f"\n{news_context}\n\n"
            f"Примечание: используй новостной контекст для подтверждения "
            f"статистических данных и упоминания реальных событий. "
            f"Если новость противоречит статистике — укажи на это."
        )

    # 6. Инструкция
    parts.append(
        "\nНа основе полных данных проведи глубокий аналитический анализ "
        "по разделам, указанным в системном промпте: динамика, корреляции, "
        "паттерны, профилирование участников, прогноз рисков, рекомендации."
    )

    return "\n\n".join(parts)


def build_question_prompt(
    question: str,
    comparison: dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
    raw_supplement: str = "",
    news_context: str = "",
    clusters_context: str = "",
    cross_tables_context: str = "",
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
    if cross_tables_context:
        prompt += f"\n\n{cross_tables_context}"
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
        "max_tokens": 8192,
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
        system_prompt = SYSTEM_PROMPT_PAID

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

    # reasoning_content — формат GLM/ZhipuAI
    # reasoning — формат DeepSeek (AItunnel)
    reasoning = (message.get("reasoning_content", "") or "") or (message.get("reasoning", "") or "")
    reasoning_source = "reasoning_content" if message.get("reasoning_content") else ("reasoning" if message.get("reasoning") else "")

    # Если content пустой — пробуем извлечь ответ из reasoning/reasoning_content
    if not content and reasoning:
        logger.warning(
            f"LLM вернул пустой content, но есть {reasoning_source} ({len(reasoning)} симв.). "
            f"Пытаюсь извлечь ответ..."
        )
        paragraphs = [p.strip() for p in reasoning.split("\n") if p.strip()]
        if paragraphs:
            content = "\n".join(paragraphs[-5:]) if len(paragraphs) > 5 else "\n".join(paragraphs)
            logger.info(f"Извлечён ответ из {reasoning_source}: {len(content)} симв.")

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
    cross_tables_context: str = "",
    provider: LLMProvider = "free",
    current_cards: list[dict[str, Any]] | None = None,
    prev_cards: list[dict[str, Any]] | None = None,
) -> str:
    """
    Генерирует аналитическое резюме с помощью LLM.

    Для бесплатного метода (free): использует агрегированные метрики + кросс-таблицы.
    Для платного метода (paid): использует полные данные участников (Файл 2).

    Args:
        raw_supplement: Дополнительные данные из сырых карточек ДТП
        news_context: Новостной контекст из открытых источников
        clusters_context: Данные об очагах концентрации ДТП
        cross_tables_context: Кросс-таблицы для бесплатного метода
        provider: "free" (ZhipuAI/GLM) или "paid" (OpenAI-совместимый)
        current_cards: Сырые карточки текущего периода (для платного метода)
        prev_cards: Сырые карточки предыдущего периода (для платного метода)

    Returns:
        Текст резюме от нейросети
    """
    if provider == "paid" and current_cards is not None:
        # Платный метод: формируем полный набор данных
        logger.info(
            f"Платный метод: формирую полные данные для LLM "
            f"(текущий: {len(current_cards)} ДТП, "
            f"прошлый: {len(prev_cards) if prev_cards else 0} ДТП)"
        )

        current_full_data = format_full_data_as_csv(current_cards, current_label)
        # Предыдущий период — тоже полные данные для глубокого сравнения
        prev_full_data = ""
        if prev_cards:
            prev_full_data = format_full_data_as_csv(prev_cards, prev_label)
        logger.info(
            f"Платный метод: данные текущего = {len(current_full_data)} симв., "
            f"предыдущего = {len(prev_full_data)} символ."
        )

        prompt = build_paid_summary_prompt(
            comparison, reg_name, current_label, prev_label,
            current_full_data=current_full_data,
            prev_full_data=prev_full_data,
            news_context=news_context,
            clusters_context=clusters_context,
        )
        return await ask_llm(user_message=prompt, provider=provider)
    else:
        # Бесплатный метод: агрегированные метрики + кросс-таблицы
        prompt = build_summary_prompt(
            comparison, reg_name, current_label, prev_label,
            raw_supplement=raw_supplement,
            news_context=news_context,
            clusters_context=clusters_context,
            cross_tables_context=cross_tables_context,
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
    cross_tables_context: str = "",
    provider: LLMProvider = "free",
) -> str:
    """
    Отвечает на вопрос пользователя по данным с помощью LLM.

    Args:
        raw_supplement: Дополнительные данные из сырых карточек ДТП
        news_context: Новостной контекст из открытых источников
        clusters_context: Данные об очагах концентрации ДТП
        cross_tables_context: Кросс-таблицы для бесплатного метода
        provider: "free" (ZhipuAI/GLM) или "paid" (OpenAI-совместимый)

    Returns:
        Текст ответа от нейросети
    """
    prompt = build_question_prompt(
        question, comparison, reg_name, current_label, prev_label,
        raw_supplement=raw_supplement,
        news_context=news_context,
        clusters_context=clusters_context,
        cross_tables_context=cross_tables_context,
    )
    return await ask_llm(user_message=prompt, provider=provider)
