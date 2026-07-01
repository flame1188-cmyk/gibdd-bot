"""
Модуль локальной статистики по географической точке.

Вычисляет статистику ДТП в заданном радиусе от точки:
  - Общее количество ДТП, погибших, раненых
  - Нетрезвые водители, пешеходы
  - Распределение по видам ДТП
  - Распределение по дорогам/улицам
  - Динамика относительно прошлого года
"""

import logging
import math
from collections import Counter
from typing import Any

from analytics import _safe_int

logger = logging.getLogger(__name__)


# ========================
# Утилиты
# ========================

def parse_coordinates(text: str) -> tuple[float, float] | None:
    """
    Парсит координаты из текста.

    Поддерживаемые форматы:
      - «55.1234, 38.5678»
      - «55.1234 38.5678»
      - «55.1234,38.5678»
      - «55.1234;38.5678»

    Возвращает (lat, lon) или None.
    """
    if not text:
        return None

    import re
    text = text.strip().strip("/").strip()

    # Ищем пару чисел с разделителем
    pattern = r"(-?\d+\.?\d*)\s*[,;\s]\s*(-?\d+\.?\d*)"
    match = re.search(pattern, text)
    if not match:
        return None

    try:
        lat = float(match.group(1))
        lon = float(match.group(2))
        # Валидация: широта [-90, 90], долгота [-180, 180]
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return (lat, lon)
    except ValueError:
        return None


def filter_cards_by_radius(
    cards: list[dict],
    lat: float,
    lon: float,
    radius_m: float,
) -> list[dict]:
    """
    Фильтрует карточки ДТП в заданном радиусе от точки.

    Использует формулу Гаверсинуса для расчёта расстояния.

    Args:
        cards: Список карточек ДТП
        lat: Широта центральной точки
        lon: Долгота центральной точки
        radius_m: Радиус поиска в метрах

    Returns:
        Список карточек в радиусе, отсортированных по расстоянию
    """
    EARTH_RADIUS_KM = 6371.0
    result = []

    for card in cards:
        try:
            card_lat = float(str(card.get("coord_w", "")).strip())
            card_lon = float(str(card.get("coord_l", "")).strip())
            if card_lat == 0 and card_lon == 0:
                continue
        except (ValueError, TypeError):
            continue

        # Гаверсинус
        lat1_r, lon1_r = math.radians(lat), math.radians(lon)
        lat2_r, lon2_r = math.radians(card_lat), math.radians(card_lon)
        dlat = lat2_r - lat1_r
        dlon = lon2_r - lon1_r
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1_r) * math.cos(lat2_r)
            * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.asin(math.sqrt(min(a, 1.0)))
        dist_m = EARTH_RADIUS_KM * c * 1000.0

        if dist_m <= radius_m:
            card["_dist_m"] = dist_m
            result.append(card)

    # Сортируем по расстоянию (ближайшие первые)
    result.sort(key=lambda c: c.get("_dist_m", 0))
    return result


# ========================
# Расчёт статистики
# ========================

def calculate_point_statistics(
    lat: float,
    lon: float,
    radius_m: float,
    current_cards: list[dict],
    prev_cards: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Вычисляет статистику ДТП в заданном радиусе от точки.

    Args:
        lat: Широта центральной точки
        lon: Долгота центральной точки
        radius_m: Радиус поиска в метрах
        current_cards: Карточки ДТП текущего периода
        prev_cards: Карточки ДТП прошлого периода (опционально)

    Returns:
        Словарь со статистикой:
        {
            "center": (lat, lon),
            "radius_m": radius_m,
            "current": {
                "total": int,
                "deaths": int,
                "injured": int,
                "alcohol": int,
                "pedestrians": int,
                "by_type": Counter,
                "by_road": Counter,
                "by_weather": Counter,
                "cards": list[dict],
            },
            "prev": { ... } | None,
        }
    """
    import math

    # Фильтруем по радиусу
    current_filtered = filter_cards_by_radius(
        current_cards, lat, lon, radius_m,
    )

    current_stats = _compute_period_stats(current_filtered)

    prev_stats = None
    prev_filtered = None
    if prev_cards:
        prev_filtered = filter_cards_by_radius(
            prev_cards, lat, lon, radius_m,
        )
        prev_stats = _compute_period_stats(prev_filtered)

    return {
        "center": (lat, lon),
        "radius_m": radius_m,
        "current": current_stats,
        "prev": prev_stats,
    }


def _compute_period_stats(cards: list[dict]) -> dict[str, Any]:
    """Вычисляет статистику для одной группы карточек."""
    total = len(cards)
    deaths = 0
    injured = 0
    alcohol_count = 0
    pedestrian_count = 0
    type_counter = Counter()
    road_counter = Counter()
    weather_counter = Counter()

    for card in cards:
        deaths += _safe_int(card.get("pog"))
        injured += _safe_int(card.get("ran"))

        # Нетрезвые водители
        ts_list = card.get("ts_info", []) or []
        for ts in ts_list:
            ts_uch_list = ts.get("ts_uch", []) or []
            for uch in ts_uch_list:
                kt = str(uch.get("kt_uch", "")).lower()
                alco = str(uch.get("alco", "")).strip()
                if kt == "водитель" and alco and alco not in ("0", "00", ""):
                    alcohol_count += 1
                    break

        # Пешеходы
        uch_list = card.get("uch_info", []) or []
        for uch in uch_list:
            kt = str(uch.get("kt_uch", "")).lower()
            if kt == "пешеход":
                pedestrian_count += 1
                break

        # Вид ДТП
        dtp_type = str(card.get("dtpv", "")).strip()
        if dtp_type:
            type_counter[dtp_type] += 1

        # Дорога
        road = str(card.get("dor", "")).strip() or str(card.get("street", "")).strip()
        if road:
            road_counter[road] += 1

        # Погода
        dor_usl = card.get("dor_usl", {}) or {}
        weather_list = dor_usl.get("spog", []) or []
        if isinstance(weather_list, list):
            for w in weather_list:
                w_str = str(w).strip()
                if w_str:
                    weather_counter[w_str] += 1

    return {
        "total": total,
        "deaths": deaths,
        "injured": injured,
        "alcohol": alcohol_count,
        "pedestrians": pedestrian_count,
        "by_type": dict(type_counter),
        "by_road": dict(road_counter),
        "by_weather": dict(weather_counter),
        "cards": cards,
    }


# ========================
# Форматирование сообщения
# ========================

RADIUS_OPTIONS = [
    (250, "~250 м"),
    (500, "500 м"),
    (1000, "1 км"),
    (3000, "3 км"),
]


def format_point_stats_message(
    stats: dict[str, Any],
    current_label: str,
    prev_label: str | None = None,
) -> str:
    """
    Формирует текстовое сообщение со статистикой по точке.

    Args:
        stats: Результат calculate_point_statistics()
        current_label: Подпись текущего периода
        prev_label: Подпись прошлого периода (если есть)

    Returns:
        Текст сообщения в HTML
    """
    lat, lon = stats["center"]
    radius_m = stats["radius_m"]
    cur = stats["current"]
    prev = stats.get("prev")

    # Форматируем радиус
    if radius_m >= 1000:
        radius_str = f"{radius_m / 1000:.0f} км"
    else:
        radius_str = f"{radius_m} м"

    lines = []
    lines.append(f"\U0001F4CD <b>Статистика по точке</b>")
    lines.append(f"<code>{lat:.5f}, {lon:.5f}</code>")
    lines.append(f"Радиус: {radius_str} | {current_label}")
    lines.append("")

    # --- Текущий период ---
    lines.append(f"<b>\U0001F4CA ДТП в радиусе:</b> {cur['total']}")
    if cur["total"] > 0:
        lines.append(f"  \u2022 Погибло: {cur['deaths']}")
        lines.append(f"  \u2022 Ранено: {cur['injured']}")
        if cur["alcohol"] > 0:
            lines.append(f"  \u2022 Нетрезвые: {cur['alcohol']}")
        if cur["pedestrians"] > 0:
            lines.append(f"  \u2022 Пешеходы: {cur['pedestrians']}")

        # По видам ДТП
        by_type = cur["by_type"]
        if by_type:
            sorted_types = sorted(by_type.items(), key=lambda x: x[1], reverse=True)
            type_str = "\n".join(
                f"  \u2022 {t} \u2014 {c}"
                for t, c in sorted_types[:5]
            )
            lines.append("")
            lines.append(f"<b>По видам ДТП:</b>")
            lines.append(type_str)

        # По дорогам
        by_road = cur["by_road"]
        if by_road:
            sorted_roads = sorted(by_road.items(), key=lambda x: x[1], reverse=True)
            road_str = "\n".join(
                f"  \u2022 {r} \u2014 {c}"
                for r, c in sorted_roads[:5]
            )
            lines.append("")
            lines.append(f"<b>По дорогам:</b>")
            lines.append(road_str)
    else:
        lines.append("")
        lines.append("ДТП в указанном радиусе не найдены.")

    # --- Динамика ---
    if prev and prev_label:
        lines.append("")
        lines.append(f"<b>\U0001F4C8 Динамика ({prev_label}):</b>")

        # ДТП
        cur_total = cur["total"]
        prev_total = prev["total"]
        if prev_total > 0:
            delta = cur_total - prev_total
            pct = round((cur_total - prev_total) / prev_total * 100, 1)
            arrow = "\u2191" if delta > 0 else ("\u2193" if delta < 0 else "\u2194")
            lines.append(
                f"  ДТП: {cur_total} \u2192 {prev_total} ({delta:+d}, {pct:+.0f}%{arrow})"
            )
        elif cur_total > 0:
            lines.append(f"  ДТП: {cur_total} \u2192 0 (было {prev_total})")
        else:
            lines.append(f"  ДТП: 0 \u2192 0")

        # Погибло
        cur_deaths = cur["deaths"]
        prev_deaths = prev["deaths"]
        delta = cur_deaths - prev_deaths
        if delta != 0:
            lines.append(f"  Погибло: {cur_deaths} \u2192 {prev_deaths} ({delta:+d})")
        else:
            lines.append(f"  Погибло: {cur_deaths} \u2192 {prev_deaths}")

        # Ранено
        cur_injured = cur["injured"]
        prev_injured = prev["injured"]
        delta = cur_injured - prev_injured
        if delta != 0:
            lines.append(f"  Ранено: {cur_injured} \u2192 {prev_injured} ({delta:+d})")
        else:
            lines.append(f"  Ранено: {cur_injured} \u2192 {prev_injured}")

        # Нетрезвые
        if cur["alcohol"] > 0 or prev["alcohol"] > 0:
            lines.append(
                f"  Нетрезвые: {cur['alcohol']} \u2192 {prev['alcohol']}"
            )

        # Пешеходы
        if cur["pedestrians"] > 0 or prev["pedestrians"] > 0:
            lines.append(
                f"  Пешеходы: {cur['pedestrians']} \u2192 {prev['pedestrians']}"
            )

    return "\n".join(lines)


# ========================
# Excel: детализация ДТП по точке
# ========================

POINT_STATS_COLUMNS = [
    # --- Место и время ---
    "Дата ДТП",
    "Время",
    "Населённый пункт",
    "Район",
    "Дорога/Улица",
    "Пикетаж",
    "Расстояние, м",
    "Широта",
    "Долгота",
    # --- Характер ДТП ---
    "Вид ДТП",
    "Кол-во ТС",
    "Кол-во участников",
    "Погибло",
    "Ранено",
    "Категории участников",
    # --- Дорожные условия ---
    "Категория дороги",
    "Состояние покрытия",
    "Погода",
    "Освещение",
    # --- Причины ---
    "Нарушения ПДД",
    # --- Транспортные средства ---
    "Типы ТС",
    "Марки/Модели ТС",
]


def get_point_stats_column_names() -> list[str]:
    """Названия колонок для Excel-файла статистики по точке."""
    return list(POINT_STATS_COLUMNS)


def _join_list(arr: Any, sep: str = "; ") -> str:
    """Склеивает список в строку через разделитель."""
    if not arr:
        return ""
    if isinstance(arr, list):
        return sep.join(str(item) for item in arr if item is not None and str(item).strip() != "")
    return str(arr).strip()


def _card_to_excel_row(card: dict) -> dict[str, str]:
    """Преобразует одну карточку ДТП в строку Excel (расширенная версия)."""
    dist_m = card.get("_dist_m", 0)
    try:
        lat = float(str(card.get("coord_w", "")).strip())
        lon = float(str(card.get("coord_l", "")).strip())
    except (ValueError, TypeError):
        lat, lon = 0, 0

    # --- Пикетаж: км + м ---
    km = str(card.get("km", "")).strip()
    m = str(card.get("m", "")).strip()
    if km and m:
        piketazh = f"{km} км {m} м"
    elif km:
        piketazh = f"{km} км"
    elif m:
        piketazh = f"{m} м"
    else:
        piketazh = ""

    # --- Дорожные условия ---
    dor_usl = card.get("dor_usl", {}) or {}
    weather = _join_list(dor_usl.get("spog", []))
    osveshchenie = str(dor_usl.get("osv", "")).strip()
    s_pch = str(dor_usl.get("s_pch", "")).strip()

    # --- Участники: категории ---
    all_kt = []
    ts_list = card.get("ts_info", []) or []
    for ts in ts_list:
        for uch in (ts.get("ts_uch", []) or []):
            kt = str(uch.get("kt_uch", "")).strip()
            if kt:
                all_kt.append(kt)
    for uch in (card.get("uch_info", []) or []):
        kt = str(uch.get("kt_uch", "")).strip()
        if kt:
            all_kt.append(kt)
    # Убираем дубли
    categories = "; ".join(dict.fromkeys(all_kt))

    # --- Нарушения ПДД (непосредственные) ---
    all_npdd = []
    for ts in ts_list:
        for uch in (ts.get("ts_uch", []) or []):
            npdd = _join_list(uch.get("npdd", []))
            if npdd:
                all_npdd.append(npdd)
    for uch in (card.get("uch_info", []) or []):
        npdd = _join_list(uch.get("npdd", []))
        if npdd:
            all_npdd.append(npdd)
    npdd_str = "; ".join(all_npdd) if all_npdd else ""

    # --- Транспортные средства ---
    ts_types = []
    ts_marks = []
    for ts in ts_list:
        t = str(ts.get("t_ts", "")).strip()
        if t:
            ts_types.append(t)
        marka = str(ts.get("marka_ts", "")).strip()
        model = str(ts.get("m_ts", "")).strip()
        if marka:
            ts_marks.append(f"{marka} {model}".strip())
        elif model:
            ts_marks.append(model)

    return {
        # --- Место и время ---
        "Дата ДТП": str(card.get("date_dtp", "")).strip(),
        "Время": str(card.get("time", "")).strip(),
        "Населённый пункт": str(card.get("np", "")).strip(),
        "Район": str(card.get("district", "")).strip(),
        "Дорога/Улица": (
            str(card.get("dor", "")).strip()
            or str(card.get("street", "")).strip()
        ),
        "Пикетаж": piketazh,
        "Расстояние, м": f"{dist_m:.0f}" if dist_m > 0 else "",
        "Широта": f"{lat:.6f}" if lat else "",
        "Долгота": f"{lon:.6f}" if lon else "",
        # --- Характер ДТП ---
        "Вид ДТП": str(card.get("dtpv", "")).strip(),
        "Кол-во ТС": str(_safe_int(card.get("k_ts"))),
        "Кол-во участников": str(_safe_int(card.get("k_uch"))),
        "Погибло": str(_safe_int(card.get("pog"))),
        "Ранено": str(_safe_int(card.get("ran"))),
        "Категории участников": categories,
        # --- Дорожные условия ---
        "Категория дороги": str(card.get("dor_k", "")).strip(),
        "Состояние покрытия": s_pch,
        "Погода": weather,
        "Освещение": osveshchenie,
        # --- Причины ---
        "Нарушения ПДД": npdd_str,
        # --- Транспортные средства ---
        "Типы ТС": "; ".join(ts_types),
        "Марки/Модели ТС": "; ".join(ts_marks),
    }


def build_point_stats_excel_data(
    current_cards: list[dict],
    prev_cards: list[dict] | None,
    current_label: str,
    prev_label: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """
    Строит данные для Excel-файла с ДТП в радиусе точки.

    Возвращает кортеж из двух списков:
      (текущий_период, прошлый_период)
    Каждый список — список словарей для строк Excel.
    """
    current_rows = [_card_to_excel_row(card) for card in current_cards]

    prev_rows = []
    if prev_cards:
        prev_rows = [_card_to_excel_row(card) for card in prev_cards]

    return current_rows, prev_rows
