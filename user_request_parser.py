"""
Парсер естественного языка для запросов пользователей.

Распознаёт из текста:
  1. Название региона → код региона
  2. Период (год, квартал, N месяцев, конкретный месяц)

Поддерживаемые форматы:
  "Вологодская область за 2025 год"
  "Вологодская за 3 месяца 2026"
  "март 2025 Вологодская"
  "Алтайский край за I квартал 2025"
  "за полугодие 2025 Республика Татарстан"
  "2.2024 1101"  (строгий формат тоже работает)
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ========================
# Data classes
# ========================

@dataclass(frozen=True)
class ParsedPeriod:
    """Распознанный период."""
    months: list[int]  # Список номеров месяцев (1-12)
    year: int          # Год
    label: str         # Человекочитаемое описание

    def get_dat_list(self) -> list[str]:
        """Возвращает список строк в формате m.YYYY для API."""
        return [f"{m}.{self.year}" for m in self.months]


@dataclass(frozen=True)
class ParsedRequest:
    """Результат парсинга запроса пользователя."""
    region_code: str
    region_name: str
    period: ParsedPeriod


# ========================
# Справочник месяцев
# ========================

MONTH_NAMES = {
    # Полные названия
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4,
    "май": 5, "июнь": 6, "июль": 7, "август": 8,
    "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
    # Краткие названия
    "янв": 1, "фев": 2, "мар": 3, "апр": 4,
    "июн": 6, "июл": 7, "авг": 8, "сен": 9,
    "окт": 10, "ноя": 11, "дек": 12,
    # Римские месяцы (редко, но бывает)
    "i": 1, "ii": 2, "iii": 3, "iv": 4,
    "v": 5, "vi": 6, "vii": 7, "viii": 8,
    "ix": 9, "x": 10, "xi": 11, "xii": 12,
}

MONTH_NAMES_GENITIVE = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# Объединяем все варианты
ALL_MONTH_VARIANTS = {**MONTH_NAMES, **MONTH_NAMES_GENITIVE}

# Названия кварталов
QUARTER_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "1": 1, "2": 2, "3": 3, "4": 4}

QUARTER_NAMES = {
    "первый": 1, "второй": 2, "третий": 3, "четвёртый": 4, "четвертый": 4,
}


# ========================
# Кэш справочника регионов
# ========================

_regions_cache: list[dict[str, str]] | None = None


async def ensure_regions_loaded() -> list[dict[str, str]]:
    """Загружает справочник регионов при первом обращении (с кэшированием)."""
    global _regions_cache
    if _regions_cache is not None:
        return _regions_cache

    from api_client import fetch_regions
    try:
        _regions_cache = await fetch_regions()
        logger.info(f"Справочник регионов загружен: {len(_regions_cache)} записей")
    except Exception as e:
        logger.error(f"Не удалось загрузить справочник регионов: {e}")
        _regions_cache = []
    return _regions_cache


def _build_search_index(regions: list[dict[str, str]]) -> list[tuple[str, str, str]]:
    """
    Строит поисковый индекс: список (код, полное_название, нормализованное_название).
    Нормализация: нижний регистр, удаление лишних слов.
    """
    index = []
    for r in regions:
        code = r["code"]
        name = r["name"]
        # Нормализуем: нижний регистр
        normalized = name.lower().strip()
        # Создаём вариант без "республика", "область" и т.д. для поиска
        shortened = normalized
        for prefix in ["республика ", "респ. ", "автономная область ",
                       "автономный округ ", "край ", "область ", "обл. "]:
            if shortened.startswith(prefix):
                shortened = shortened[len(prefix):].strip()
                break
        # Убираем "г. " для городов федерального значения
        if shortened.startswith("г. "):
            shortened = shortened[3:].strip()

        index.append((code, name, normalized))
        if shortened != normalized:
            index.append((code, name, shortened))

    return index


def find_region(text: str, regions: list[dict[str, str]]) -> tuple[str, str] | None:
    """
    Ищет регион по тексту. Возвращает (код, название) или None.

    Стратегии поиска (по приоритету):
      1. Точное совпадение по коду (4+ цифры)
      2. Точное совпадение по названию
      3. Вхождение названия региона в текст
      4. Вхождение текста в название региона
      5. Поиск по сокращённому названию
    """
    text_lower = text.lower().strip()
    search_index = _build_search_index(regions)

    # 1. Поиск по коду региона (цифры в запросе)
    code_match = re.search(r"\b(\d{2,4})\b", text)
    if code_match:
        code = code_match.group(1)
        for code_r, name_r, _ in search_index:
            if code_r == code:
                return code_r, name_r

    # 2-5. Поиск по названию
    best_match = None
    best_score = 0

    for code_r, name_r, normalized in search_index:
        # Пропускаем дубликаты кодов (ищем только по первому вхождению каждого кода)
        if best_match and best_match[0] == code_r:
            continue

        score = 0

        # Точное совпадение
        if normalized == text_lower:
            score = 100
        # Точное совпадение по полному названию
        elif name_r.lower() == text_lower:
            score = 95
        # Название региона содержится в тексте запроса
        elif normalized in text_lower:
            score = 80 + len(normalized)  # Предпочитаем более длинные совпадения
        # Текст запроса содержится в названии региона
        elif text_lower in normalized:
            score = 60 + len(text_lower)
        # Проверяем каждое слово из запроса
        else:
            words = text_lower.split()
            for word in words:
                if len(word) < 3:
                    continue  # Пропускаем слишком короткие слова
                if word in normalized:
                    score += 30 + len(word)

        if score > best_score:
            best_score = score
            best_match = (code_r, name_r)

    # Минимальный порог совпадения
    if best_match and best_score >= 30:
        return best_match

    return None


# ========================
# Парсинг периода
# ========================

def parse_period(text: str) -> ParsedPeriod | None:
    """
    Извлекает период из текста запроса.

    Returns:
        ParsedPeriod или None, если не удалось распознать
    """
    text_lower = text.lower().strip()
    current_year = __import__("datetime").datetime.now().year

    # --- Весь год ---
    m = re.search(r"за?\s*(весь\s*)?(\d{4})\s*год", text_lower)
    if m:
        year = int(m.group(2))
        return ParsedPeriod(
            months=list(range(1, 13)),
            year=year,
            label=f"Весь {year} год",
        )

    m = re.search(r"за?\s*(весь\s*)?(\d{4})", text_lower)
    if m:
        year = int(m.group(2))
        return ParsedPeriod(
            months=list(range(1, 13)),
            year=year,
            label=f"Весь {year} год",
        )

    # --- Квартал ---
    m = re.search(
        r"(?:за\s*)?"
        r"(?:(i{1,2}v?|vi{0,3}|iv|v|ix|x{1,3})\s*(?:кв|квартал))\s*"
        r"(\d{4})",
        text_lower,
    )
    if not m:
        m = re.search(
            r"(?:за\s*)?"
            r"(?:квартал\s*)"
            r"(\d{1,2})\s*"
            r"(\d{4})",
            text_lower,
        )
    if m:
        q_str = m.group(1)
        year = int(m.group(2))
        q_num = QUARTER_ROMAN.get(q_str)
        if q_num is None:
            q_num = QUARTER_NAMES.get(q_str)
        if q_num and 1 <= q_num <= 4:
            start = (q_num - 1) * 3 + 1
            end = start + 2
            quarter_roman = ["I", "II", "III", "IV"][q_num - 1]
            month_names = _month_range_label(start, end)
            return ParsedPeriod(
                months=list(range(start, end + 1)),
                year=year,
                label=f"{quarter_roman} квартал {year} ({month_names})",
            )

    # --- Полугодие ---
    m = re.search(r"(?:за\s*)?(первое|второе|1|2)\s*полугодие\s*(\d{4})", text_lower)
    if m:
        half_str = m.group(1)
        year = int(m.group(2))
        if half_str in ("первое", "1"):
            months = list(range(1, 7))
            label = f"Первое полугодие {year} (Янв-Июн)"
        else:
            months = list(range(7, 13))
            label = f"Второе полугодие {year} (Июл-Дек)"
        return ParsedPeriod(months=months, year=year, label=label)

    # --- N месяцев ---
    m = re.search(r"за?\s*(\d{1,2})\s*месяц(?:ев|а)?\s*(\d{4})", text_lower)
    if m:
        n = int(m.group(1))
        year = int(m.group(2))
        if 1 <= n <= 12:
            months = list(range(1, n + 1))
            month_names = _month_range_label(1, n)
            return ParsedPeriod(
                months=months,
                year=year,
                label=f"{n} мес. {year} ({month_names})",
            )

    # --- Конкретный месяц ---
    for month_name, month_num in ALL_MONTH_VARIANTS.items():
        # Сначала проверяем более длинные названия (чтобы "сентябрь" не матчился раньше "сен")
        if month_name in text_lower:
            # Ищем год рядом с названием месяца
            # Ищем год ПОСЛЕ названия месяца
            pos = text_lower.index(month_name)
            after = text_lower[pos + len(month_name):]
            m_year = re.search(r"(\d{4})", after)
            if m_year:
                year = int(m_year.group(1))
            else:
                # Ищем год ДО названия месяца
                before = text_lower[:pos]
                m_year = re.search(r"(\d{4})", before)
                if m_year:
                    year = int(m_year.group(1))
                else:
                    year = current_year

            month_display = _get_month_name_russian(month_num)
            return ParsedPeriod(
                months=[month_num],
                year=year,
                label=f"{month_display} {year}",
            )

    # --- Только год (4 цифры) ---
    m = re.search(r"\b(20\d{2})\b", text_lower)
    if m:
        year = int(m.group(1))
        if 2000 <= year <= 2100:
            return ParsedPeriod(
                months=list(range(1, 13)),
                year=year,
                label=f"Весь {year} год",
            )

    return None


def _month_range_label(start: int, end: int) -> str:
    """Возвращает 'Янв-Мар' для диапазона месяцев."""
    month_short = {
        1: "Янв", 2: "Фев", 3: "Мар", 4: "Апр", 5: "Май", 6: "Июн",
        7: "Июл", 8: "Авг", 9: "Сен", 10: "Окт", 11: "Ноя", 12: "Дек",
    }
    return f"{month_short.get(start, '')}-{month_short.get(end, '')}"


def _get_month_name_russian(num: int) -> str:
    """Возвращает название месяца в именительном падеже."""
    names = {
        1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
        5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
        9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
    }
    return names.get(num, "")


# ========================
# Главный парсер
# ========================

async def parse_user_message(text: str) -> ParsedRequest | None:
    """
    Парсит текстовое сообщение пользователя и извлекает регион + период.

    Returns:
        ParsedRequest или None, если не удалось распознать
    """
    text = text.strip()
    if not text:
        return None

    # Сначала пробуем строгий формат: "2.2024 1101"
    strict = _parse_strict_format(text)
    if strict:
        return strict

    # Загружаем справочник регионов
    regions = await ensure_regions_loaded()
    if not regions:
        return None

    # Ищем регион
    region = find_region(text, regions)
    if region is None:
        return None

    region_code, region_name = region

    # Парсим период
    period = parse_period(text)
    if period is None:
        return None

    return ParsedRequest(
        region_code=region_code,
        region_name=region_name,
        period=period,
    )


def _parse_strict_format(text: str) -> ParsedRequest | None:
    """Парсит строгий формат: m.YYYY RRRR (например, '2.2024 1101')."""
    m = re.match(r"^(\d{1,2}\.\d{4})\s+(\d{2,4})$", text.strip())
    if not m:
        return None

    dat = m.group(1)
    reg = m.group(2)

    if reg == "1100":
        return None

    dat_m = re.match(r"^(\d{1,2})\.(\d{4})$", dat)
    if not dat_m:
        return None

    month = int(dat_m.group(1))
    year = int(dat_m.group(2))
    if month < 1 or month > 12:
        return None

    month_display = _get_month_name_russian(month)
    return ParsedRequest(
        region_code=reg,
        region_name=f"Регион {reg}",
        period=ParsedPeriod(
            months=[month],
            year=year,
            label=f"{month_display} {year}",
        ),
    )
