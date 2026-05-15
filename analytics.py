"""
Модуль аналитики ДТП: сравнение текущего периода с аналогичным периодом прошлого года.

Вычисляет ключевые метрики:
  - Всего ДТП, погибших, раненых
  - ДТП с участием нетрезвых водителей
  - ДТП с пешеходами
  - Распределение по дням недели, часам, видам ДТП
  - Процентные изменения между периодами
"""

import logging
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)


# ========================
# Названия дней недели и часов
# ========================

DAY_NAMES = {
    0: "Понедельник", 1: "Вторник", 2: "Среда",
    3: "Четверг", 4: "Пятница", 5: "Суббота", 6: "Воскресенье",
}

DAY_SHORT = {
    0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс",
}


# ========================
# Подсчёт метрик по карточкам ДТП
# ========================

def _safe_int(val: Any) -> int:
    """Безопасное приведение к int."""
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _safe_float(val: Any) -> float:
    """Безопасное приведение к float."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _get_hour(time_str: str) -> int | None:
    """Извлекает час из строки времени (формат 'HH:MM' или 'H:MM')."""
    if not time_str:
        return None
    try:
        parts = time_str.strip().split(":")
        hour = int(parts[0])
        if 0 <= hour <= 23:
            return hour
        return None
    except (ValueError, IndexError):
        return None


def _get_weekday(date_str: str) -> int | None:
    """Извлекает день недели из строки даты (формат 'DD.MM.YYYY')."""
    if not date_str:
        return None
    try:
        from datetime import datetime
        dt = datetime.strptime(date_str.strip()[:10], "%d.%m.%Y")
        return dt.weekday()  # 0=Пн, 6=Вс
    except (ValueError, IndexError):
        return None


def _has_alcohol(card: dict) -> bool:
    """Проверяет, есть ли в ДТП нетрезвый участник."""
    # Проверяем водителей из ts_info
    ts_list = card.get("ts_info", []) or []
    for ts in ts_list:
        ts_uch_list = ts.get("ts_uch", []) or []
        for uch in ts_uch_list:
            kt = str(uch.get("kt_uch", "")).lower()
            alco = str(uch.get("alco", "")).strip()
            if kt == "водитель" and alco and alco not in ("0", "00", ""):
                return True
    return False


def _has_pedestrian(card: dict) -> bool:
    """Проверяет, есть ли в ДТП пешеход."""
    uch_list = card.get("uch_info", []) or []
    for uch in uch_list:
        kt = str(uch.get("kt_uch", "")).lower()
        if kt == "пешеход":
            return True
    return False


def calculate_metrics(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Считает все метрики по списку карточек ДТП.

    Returns:
        Словарь с метриками:
          - total: всего ДТП
          - deaths: погибших
          - injured: раненых
          - alcohol: ДТП с нетрезвыми водителями
          - pedestrians: ДТП с пешеходами
          - deaths_per_100: погибших на 100 ДТП
          - injured_per_100: раненых на 100 ДТП
          - by_weekday: {0: count, 1: count, ...}
          - by_hour: {0: count, 1: count, ...}
          - by_type: {вид_ДТП: count, ...}
          - by_weather: {погода: count, ...}
    """
    total = len(cards)
    deaths = 0
    injured = 0
    alcohol_count = 0
    pedestrian_count = 0

    weekday_counter = Counter()
    hour_counter = Counter()
    type_counter = Counter()
    weather_counter = Counter()

    for card in cards:
        # Погибшие и раненые
        deaths += _safe_int(card.get("pog"))
        injured += _safe_int(card.get("ran"))

        # Нетрезвые водители
        if _has_alcohol(card):
            alcohol_count += 1

        # Пешеходы
        if _has_pedestrian(card):
            pedestrian_count += 1

        # День недели
        wd = _get_weekday(str(card.get("date_dtp", "")))
        if wd is not None:
            weekday_counter[wd] += 1

        # Час
        hour = _get_hour(str(card.get("time", "")))
        if hour is not None:
            hour_counter[hour] += 1

        # Вид ДТП
        dtp_type = str(card.get("dtpv", "")).strip()
        if dtp_type:
            type_counter[dtp_type] += 1

        # Погодные условия
        dor_usl = card.get("dor_usl", {}) or {}
        weather_list = dor_usl.get("spog", []) or []
        if isinstance(weather_list, list):
            for w in weather_list:
                w_str = str(w).strip()
                if w_str:
                    weather_counter[w_str] += 1

    deaths_per_100 = round(deaths / total * 100, 1) if total > 0 else 0
    injured_per_100 = round(injured / total * 100, 1) if total > 0 else 0

    return {
        "total": total,
        "deaths": deaths,
        "injured": injured,
        "alcohol": alcohol_count,
        "pedestrians": pedestrian_count,
        "deaths_per_100": deaths_per_100,
        "injured_per_100": injured_per_100,
        "by_weekday": dict(weekday_counter),
        "by_hour": dict(hour_counter),
        "by_type": dict(type_counter),
        "by_weather": dict(weather_counter),
    }


def compare_metrics(
    current: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, Any]:
    """
    Сравнивает метрики текущего и предыдущего периода.

    Returns:
        Словарь с результатами сравнения.
    """
    def pct_change(new: float, old: float) -> float:
        """Вычисляет процент изменения."""
        if old == 0:
            return 0.0 if new == 0 else 100.0
        return round((new - old) / old * 100, 1)

    result = {
        "total": {
            "current": current["total"],
            "previous": previous["total"],
            "change": pct_change(current["total"], previous["total"]),
            "abs_change": current["total"] - previous["total"],
        },
        "deaths": {
            "current": current["deaths"],
            "previous": previous["deaths"],
            "change": pct_change(current["deaths"], previous["deaths"]),
            "abs_change": current["deaths"] - previous["deaths"],
        },
        "injured": {
            "current": current["injured"],
            "previous": previous["injured"],
            "change": pct_change(current["injured"], previous["injured"]),
            "abs_change": current["injured"] - previous["injured"],
        },
        "alcohol": {
            "current": current["alcohol"],
            "previous": previous["alcohol"],
            "change": pct_change(current["alcohol"], previous["alcohol"]),
            "abs_change": current["alcohol"] - previous["alcohol"],
        },
        "pedestrians": {
            "current": current["pedestrians"],
            "previous": previous["pedestrians"],
            "change": pct_change(current["pedestrians"], previous["pedestrians"]),
            "abs_change": current["pedestrians"] - previous["pedestrians"],
        },
        "deaths_per_100": {
            "current": current["deaths_per_100"],
            "previous": previous["deaths_per_100"],
            "change": round(current["deaths_per_100"] - previous["deaths_per_100"], 1),
            "abs_change": round(current["deaths_per_100"] - previous["deaths_per_100"], 1),
        },
        "injured_per_100": {
            "current": current["injured_per_100"],
            "previous": previous["injured_per_100"],
            "change": round(current["injured_per_100"] - previous["injured_per_100"], 1),
            "abs_change": round(current["injured_per_100"] - previous["injured_per_100"], 1),
        },
    }

    # Распределения
    result["by_weekday"] = {
        "current": current["by_weekday"],
        "previous": previous["by_weekday"],
    }
    result["by_hour"] = {
        "current": current["by_hour"],
        "previous": previous["by_hour"],
    }
    result["by_type"] = {
        "current": current["by_type"],
        "previous": previous["by_type"],
    }
    result["by_weather"] = {
        "current": current["by_weather"],
        "previous": previous["by_weather"],
    }

    return result


def format_change(value: float) -> str:
    """Форматирует процент изменения со знаком и стрелкой."""
    if value > 0:
        return f"+{value}% \u2191"
    elif value < 0:
        return f"{value}% \u2193"
    else:
        return "0% \u2194"


def build_analytics_message(
    comparison: dict[str, Any],
    reg_name: str,
    current_label: str,
    previous_label: str,
) -> str:
    """
    Формирует текстовое сообщение с результатами анализа.

    Args:
        comparison: Результат compare_metrics()
        reg_name: Название региона
        current_label: Подпись текущего периода
        previous_label: Подпись предыдущего периода

    Returns:
        Текст сообщения в Markdown
    """
    lines = []
    lines.append(f"\U0001F4CA <b>АНАЛИТИКА: {reg_name}</b>")
    lines.append(f"Период: {current_label}")
    lines.append(f"Сравнение: {previous_label}")
    lines.append("")

    # Таблица основных показателей
    lines.append("<b>\u2500\u2500\u2500 Основные показатели \u2500\u2500\u2500</b>")
    lines.append("")

    metrics_table = [
        ("Всего ДТП", "total"),
        ("Погибло", "deaths"),
        ("Ранено", "injured"),
        ("ДТП с нетрезвыми", "alcohol"),
        ("ДТП с пешеходами", "pedestrians"),
        ("Погибло на 100 ДТП", "deaths_per_100"),
        ("Ранено на 100 ДТП", "injured_per_100"),
    ]

    for label, key in metrics_table:
        m = comparison[key]
        change = format_change(m["change"])
        abs_change = m["abs_change"]
        if abs_change > 0:
            abs_str = f"(+{abs_change})"
        elif abs_change < 0:
            abs_str = f"({abs_change})"
        else:
            abs_str = "(=)"
        lines.append(f"<b>{label}:</b> {m['current']} | {change} {abs_str}")

    lines.append("")

    # Пиковый день недели
    lines.append("<b>\u2500\u2500\u2500 По дням недели \u2500\u2500\u2500</b>")
    lines.append("")
    cur_wd = comparison["by_weekday"]["current"]
    prev_wd = comparison["by_weekday"]["previous"]

    if cur_wd:
        sorted_days = sorted(cur_wd.items(), key=lambda x: x[1], reverse=True)
        peak_day_num, peak_day_count = sorted_days[0]
        peak_day_name = DAY_SHORT.get(peak_day_num, str(peak_day_num))

        total_current = sum(cur_wd.values())
        avg_per_day = total_current / 7 if total_current > 0 else 0
        pct_of_avg = round(peak_day_count / avg_per_day * 100, 0) if avg_per_day > 0 else 0

        lines.append(f"Пиковый день: <b>{DAY_NAMES.get(peak_day_num, '')}</b> ({peak_day_count} ДТП, {pct_of_avg}% от среднего)")

        # Таблица по дням
        for day_num in range(7):
            day_name = DAY_SHORT[day_num]
            cur = cur_wd.get(day_num, 0)
            prv = prev_wd.get(day_num, 0)
            if prv > 0:
                change = round((cur - prv) / prv * 100, 1)
                arrow = "\u2191" if change > 0 else ("\u2193" if change < 0 else "\u2194")
                lines.append(f"  {day_name}: {cur} ({change:+.0f}%{arrow})")
            else:
                lines.append(f"  {day_name}: {cur}")
    else:
        lines.append("Нет данных для анализа по дням недели")

    lines.append("")

    # Пиковый час
    lines.append("<b>\u2500\u2500\u2500 По часам суток \u2500\u2500\u2500</b>")
    lines.append("")

    cur_hour = comparison["by_hour"]["current"]
    prev_hour = comparison["by_hour"]["previous"]

    if cur_hour:
        # Группируем по 3-часовым интервалам
        intervals = {}
        for h in range(24):
            interval_start = (h // 3) * 3
            interval_end = interval_start + 2
            interval_key = f"{interval_start:02d}-{interval_end:02d}"
            intervals.setdefault(interval_key, 0)
            intervals[interval_key] += cur_hour.get(h, 0)

        sorted_intervals = sorted(intervals.items(), key=lambda x: x[1], reverse=True)
        peak_interval, peak_count = sorted_intervals[0]

        total_current = sum(cur_hour.values())
        avg_per_interval = total_current / 8 if total_current > 0 else 0
        pct_of_avg = round(peak_count / avg_per_interval * 100, 0) if avg_per_interval > 0 else 0

        lines.append(f"Пиковый интервал: <b>{peak_interval}</b> ({peak_count} ДТП, {pct_of_avg}% от среднего)")

        # Топ-3 опасных часа
        sorted_hours = sorted(cur_hour.items(), key=lambda x: x[1], reverse=True)
        top_hours = sorted_hours[:3]
        hours_str = ", ".join(f"{h:02d}:00 ({c})" for h, c in top_hours)
        lines.append(f"Топ-3 часа: {hours_str}")
    else:
        lines.append("Нет данных для анализа по часам")

    lines.append("")

    # Типы ДТП
    lines.append("<b>\u2500\u2500\u2500 По видам ДТП \u2500\u2500\u2500</b>")
    lines.append("")

    cur_type = comparison["by_type"]["current"]
    prev_type = comparison["by_type"]["previous"]

    if cur_type:
        sorted_types = sorted(cur_type.items(), key=lambda x: x[1], reverse=True)
        for tp_name, tp_count in sorted_types[:7]:
            prv = prev_type.get(tp_name, 0)
            if prv > 0:
                change = round((tp_count - prv) / prv * 100, 1)
                arrow = "\u2191" if change > 0 else ("\u2193" if change < 0 else "\u2194")
                lines.append(f"  {tp_name}: {tp_count} ({change:+.0f}%{arrow})")
            else:
                lines.append(f"  {tp_name}: {tp_count}")
    else:
        lines.append("Нет данных для анализа по видам ДТП")

    lines.append("")

    # Вывод
    lines.append("<b>\u2500\u2500\u2500 Вывод \u2500\u2500\u2500</b>")
    lines.append("")

    total_change = comparison["total"]["change"]
    deaths_change = comparison["deaths"]["change"]
    alcohol_change = comparison["alcohol"]["change"]
    ped_change = comparison["pedestrians"]["change"]

    # Общая оценка
    if total_change <= -5:
        lines.append(f"\u2705 Общее количество ДТП снизилось на {abs(total_change):.1f}% \u2014 положительная динамика.")
    elif total_change >= 5:
        lines.append(f"\u26A0\uFE0F Общее количество ДТП выросло на {total_change:.1f}% \u2014 отрицательная динамика.")
    else:
        lines.append(f"\u2194 Общее количество ДТП осталось на прежнем уровне (изменение {total_change:+.1f}%).")

    # Погибшие
    if deaths_change < 0:
        lines.append(f"\u2705 Число погибших снизилось на {abs(deaths_change):.1f}%.")
    elif deaths_change > 0:
        lines.append(f"\u274C Число погибших выросло на {deaths_change:.1f}% \u2014 требует внимания.")

    # Нетрезвые
    if alcohol_change > 5:
        lines.append(f"\U0001F976 Доля ДТП с нетрезвыми водителями выросла на {alcohol_change:.1f}%.")

    # Пешеходы
    if ped_change > 5:
        lines.append(f"\U0001F6B6 ДТП с пешеходами выросли на {ped_change:.1f}% \u2014 требует внимания.")

    return "\n".join(lines)


def build_analytics_excel_data(
    comparison: dict[str, Any],
    reg_name: str,
    current_label: str,
    previous_label: str,
) -> list[dict[str, str]]:
    """
    Строит данные для Excel-файла аналитики.

    Returns:
        Список словарей с данными для таблицы
    """
    rows = []

    # Заголовок
    rows.append({
        "Показатель": "РЕГИОН",
        current_label: reg_name,
        previous_label: reg_name,
        "Изменение, %": "",
        "Изменение, абс.": "",
    })

    # Основные метрики
    metrics = [
        ("Всего ДТП", "total"),
        ("Погибло, чел.", "deaths"),
        ("Ранено, чел.", "injured"),
        ("ДТП с нетрезвыми водителями", "alcohol"),
        ("ДТП с пешеходами", "pedestrians"),
        ("Погибло на 100 ДТП", "deaths_per_100"),
        ("Ранено на 100 ДТП", "injured_per_100"),
    ]

    for label, key in metrics:
        m = comparison[key]
        cur = m["current"]
        prv = m["previous"]
        change = m["change"]
        abs_change = m["abs_change"]
        rows.append({
            "Показатель": label,
            current_label: cur,
            previous_label: prv,
            "Изменение, %": change,
            "Изменение, абс.": abs_change,
        })

    # Пустая строка-разделитель
    rows.append({
        "Показатель": "",
        current_label: "",
        previous_label: "",
        "Изменение, %": "",
        "Изменение, абс.": "",
    })

    # По дням недели
    rows.append({
        "Показатель": "ПО ДНЯМ НЕДЕЛИ",
        current_label: "",
        previous_label: "",
        "Изменение, %": "",
        "Изменение, абс.": "",
    })

    cur_wd = comparison["by_weekday"]["current"]
    prev_wd = comparison["by_weekday"]["previous"]

    for day_num in range(7):
        day_name = DAY_NAMES[day_num]
        cur = cur_wd.get(day_num, 0)
        prv = prev_wd.get(day_num, 0)
        if prv > 0:
            change = round((cur - prv) / prv * 100, 1)
        else:
            change = 0
        rows.append({
            "Показатель": day_name,
            current_label: cur,
            previous_label: prv,
            "Изменение, %": change,
            "Изменение, абс.": cur - prv,
        })

    # Пустая строка-разделитель
    rows.append({
        "Показатель": "",
        current_label: "",
        previous_label: "",
        "Изменение, %": "",
        "Изменение, абс.": "",
    })

    # По часам суток (интервалы по 3 часа)
    rows.append({
        "Показатель": "ПО ЧАСАМ СУТОК (интервалы 3 ч)",
        current_label: "",
        previous_label: "",
        "Изменение, %": "",
        "Изменение, абс.": "",
    })

    cur_hour = comparison["by_hour"]["current"]
    prev_hour = comparison["by_hour"]["previous"]

    for interval_start in range(0, 24, 3):
        interval_end = interval_start + 2
        interval_label = f"{interval_start:02d}:00 - {interval_end:02d}:59"

        cur = sum(cur_hour.get(h, 0) for h in range(interval_start, interval_start + 3))
        prv = sum(prev_hour.get(h, 0) for h in range(interval_start, interval_start + 3))
        if prv > 0:
            change = round((cur - prv) / prv * 100, 1)
        else:
            change = 0
        rows.append({
            "Показатель": interval_label,
            current_label: cur,
            previous_label: prv,
            "Изменение, %": change,
            "Изменение, абс.": cur - prv,
        })

    # Пустая строка-разделитель
    rows.append({
        "Показатель": "",
        current_label: "",
        previous_label: "",
        "Изменение, %": "",
        "Изменение, абс.": "",
    })

    # По видам ДТП
    rows.append({
        "Показатель": "ПО ВИДАМ ДТП",
        current_label: "",
        previous_label: "",
        "Изменение, %": "",
        "Изменение, абс.": "",
    })

    cur_type = comparison["by_type"]["current"]
    prev_type = comparison["by_type"]["previous"]

    all_types = sorted(set(list(cur_type.keys()) + list(prev_type.keys())))
    sorted_types = sorted(all_types, key=lambda x: cur_type.get(x, 0) + prev_type.get(x, 0), reverse=True)

    for tp_name in sorted_types:
        cur = cur_type.get(tp_name, 0)
        prv = prev_type.get(tp_name, 0)
        if prv > 0:
            change = round((cur - prv) / prv * 100, 1)
        else:
            change = 0
        rows.append({
            "Показатель": tp_name,
            current_label: cur,
            previous_label: prv,
            "Изменение, %": change,
            "Изменение, абс.": cur - prv,
        })

    # Пустая строка-разделитель
    rows.append({
        "Показатель": "",
        current_label: "",
        previous_label: "",
        "Изменение, %": "",
        "Изменение, абс.": "",
    })

    # По погодным условиям
    rows.append({
        "Показатель": "ПО ПОГОДНЫМ УСЛОВИЯМ",
        current_label: "",
        previous_label: "",
        "Изменение, %": "",
        "Изменение, абс.": "",
    })

    cur_weather = comparison["by_weather"]["current"]
    prev_weather = comparison["by_weather"]["previous"]

    all_weather = sorted(set(list(cur_weather.keys()) + list(prev_weather.keys())))
    sorted_weather = sorted(all_weather, key=lambda x: cur_weather.get(x, 0) + prev_weather.get(x, 0), reverse=True)

    for w_name in sorted_weather:
        cur = cur_weather.get(w_name, 0)
        prv = prev_weather.get(w_name, 0)
        if prv > 0:
            change = round((cur - prv) / prv * 100, 1)
        else:
            change = 0
        rows.append({
            "Показатель": w_name,
            current_label: cur,
            previous_label: prv,
            "Изменение, %": change,
            "Изменение, абс.": cur - prv,
        })

    return rows


def get_analytics_column_names(
    current_label: str,
    previous_label: str,
) -> list[str]:
    """Возвращает названия колонок для Excel-файла аналитики."""
    return ["Показатель", current_label, previous_label, "Изменение, %", "Изменение, абс."]


# ============================================================
# Извлечение детальных данных из сырых карточек для LLM
# ============================================================

def _get_card_alcohol_detail(card: dict) -> str | None:
    """Извлекает детали по алкоголю из карточки."""
    ts_list = card.get("ts_info", []) or []
    for ts in ts_list:
        ts_uch_list = ts.get("ts_uch", []) or []
        for uch in ts_uch_list:
            kt = str(uch.get("kt_uch", "")).lower()
            alco = str(uch.get("alco", "")).strip()
            if kt == "водитель" and alco and alco not in ("0", "00", ""):
                return alco
    return None


def _get_card_violations(card: dict) -> list[str]:
    """Извлекает нарушения ПДД из карточки."""
    violations = []
    for ts in (card.get("ts_info", []) or []):
        for uch in (ts.get("ts_uch", []) or []):
            npdd_list = uch.get("npdd", []) or []
            if isinstance(npdd_list, list):
                violations.extend(str(v).strip() for v in npdd_list if str(v).strip())
    for uch in (card.get("uch_info", []) or []):
        npdd_list = uch.get("npdd", []) or []
        if isinstance(npdd_list, list):
            violations.extend(str(v).strip() for v in npdd_list if str(v).strip())
    return violations


def _get_card_vehicles(card: dict) -> list[str]:
    """Извлекает типы ТС из карточки."""
    vehicles = []
    for ts in (card.get("ts_info", []) or []):
        t_ts = str(ts.get("t_ts", "")).strip()
        if t_ts:
            vehicles.append(t_ts)
    return vehicles


def _get_card_road_state(card: dict) -> list[str]:
    """Извлекает состояние дороги из карточки."""
    states = []
    dor_usl = card.get("dor_usl", {}) or {}
    sdor = dor_usl.get("sdor", []) or []
    if isinstance(sdor, list):
        states.extend(str(s).strip() for s in sdor if str(s).strip())
    return states


def extract_raw_supplement(
    cards: list[dict[str, Any]],
    label: str,
    max_cards: int = 50,
) -> str:
    """
    Извлекает из сырых карточек дополнительные данные,
    которых нет в базовой агрегации.

    Включает:
      - Типы ТС (статистика)
      - Нарушения ПДД (статистика, топ-15)
      - Состояние дороги (статистика)
      - Районы/населённые пункты (топ-15)
      - Детали смертельных ДТП
      - Детали ДТП с нетрезвыми
      - Детали ДТП с пешеходами
      - Контрольные суммы по агрегации

    Args:
        cards: Список сырых карточек ДТП
        label: Подпись периода (например "I квартал 2026")
        max_cards: Максимум карточек в деталях (срез по тяжести)

    Returns:
        Текстовый блок для добавления в промпт LLM
    """
    if not cards:
        return f"\nПОДРОБНЫЕ ДАННЫЕ ({label}): нет данных\n"

    lines = []
    lines.append(f"\nПОДРОБНЫЕ ДАННЫЕ ({label}):")

    # --- Типы ТС ---
    vehicle_counter = Counter()
    for card in cards:
        for v in _get_card_vehicles(card):
            vehicle_counter[v] += 1
    if vehicle_counter:
        lines.append("\nТипы транспортных средств:")
        for v, cnt in vehicle_counter.most_common(12):
            lines.append(f"  - {v}: {cnt}")

    # --- Нарушения ПДД ---
    violation_counter = Counter()
    for card in cards:
        for v in _get_card_violations(card):
            violation_counter[v] += 1
    if violation_counter:
        lines.append("\nНарушения ПДД (топ-15):")
        for v, cnt in violation_counter.most_common(15):
            lines.append(f"  - {v}: {cnt}")

    # --- Состояние дороги ---
    road_counter = Counter()
    for card in cards:
        for r in _get_card_road_state(card):
            road_counter[r] += 1
    if road_counter:
        lines.append("\nСостояние дорожного покрытия:")
        for r, cnt in road_counter.most_common(10):
            lines.append(f"  - {r}: {cnt}")

    # --- Районы / населённые пункты ---
    district_counter = Counter()
    for card in cards:
        d = str(card.get("district", "")).strip()
        np_val = str(card.get("np", "")).strip()
        loc = d if d else np_val
        if loc:
            district_counter[loc] += 1
    if district_counter:
        lines.append("\nРайоны/населённые пункты (топ-15):")
        for d, cnt in district_counter.most_common(15):
            lines.append(f"  - {d}: {cnt}")

    # --- Детали по категории: смертельные, алкоголь, пешеходы ---
    fatal_cards = [c for c in cards if _safe_int(c.get("pog")) > 0]
    alcohol_cards = [c for c in cards if _has_alcohol(c)]
    ped_cards = [c for c in cards if _has_pedestrian(c)]

    # Собираем уникальные ID, чтобы не дублировать
    detailed_ids = set()
    detailed_cards = []

    # Приоритет: смертельные + алкоголь > смертельные > алкоголь > пешеходные
    priority_cards = []
    for c in fatal_cards:
        if c.get("empt_number") not in detailed_ids:
            detailed_ids.add(c.get("empt_number"))
            priority_cards.append((c, 0 if _has_alcohol(c) else 1))
    for c in alcohol_cards:
        if c.get("empt_number") not in detailed_ids:
            detailed_ids.add(c.get("empt_number"))
            priority_cards.append((c, 2))
    for c in ped_cards:
        if c.get("empt_number") not in detailed_ids:
            detailed_ids.add(c.get("empt_number"))
            priority_cards.append((c, 3))

    # Сортируем по приоритету и берём max_cards
    priority_cards.sort(key=lambda x: x[1])
    detailed_cards = [c for c, _ in priority_cards[:max_cards]]

    if detailed_cards:
        lines.append(f"\nДетали ДТП (смертельные/алкогольные/с пешеходами, {len(detailed_cards)} шт.):")
        for card in detailed_cards:
            date = str(card.get("date_dtp", "")).strip()
            time = str(card.get("time", "")).strip()
            dtp_type = str(card.get("dtpv", "")).strip()
            deaths = _safe_int(card.get("pog"))
            injured = _safe_int(card.get("ran"))
            district = str(card.get("district", "")).strip() or str(card.get("np", "")).strip()
            street = str(card.get("street", "")).strip()
            viol = ", ".join(_get_card_violations(card)[:3])
            alco = _get_card_alcohol_detail(card)
            vehicles = ", ".join(_get_card_vehicles(card)[:3])
            road = ", ".join(_get_card_road_state(card)[:2])

            tags = []
            if deaths > 0:
                tags.append(f"погибло={deaths}")
            if injured > 0:
                tags.append(f"ранено={injured}")
            if alco:
                tags.append(f"алкоголь={alco} промилле")
            if _has_pedestrian(card):
                tags.append("пешеход")

            location = district
            if street:
                location = f"{location}, {street}" if location else street

            line = f"  [{date} {time}] {dtp_type} | {'; '.join(tags)}"
            if location:
                line += f" | {location}"
            if viol:
                line += f" | нарушение: {viol}"
            if vehicles:
                line += f" | ТС: {vehicles}"
            if road:
                line += f" | дорога: {road}"
            lines.append(line)

    text = "\n".join(lines)
    logger.info(f"Raw supplement для LLM ({label}): {len(cards)} карточек, {len(text)} символов")
    return text
